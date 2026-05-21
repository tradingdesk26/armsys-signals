"""
Quote engine — produces signed EIP-712 loan quotes for InterAgentRepo.sol.

Three modes:
    compute_rate(P, C, T)           → fair rate given collateral + duration
    compute_collateral(P, r, T)     → required collateral given rate + duration
    compute_max_duration(P, C, r)   → max safe T given collateral + rate

All three use the same underlying calibrator (variance + regime + base anchor).
Output includes the rate, the LTV, the EIP-712 signature, and full decomposition
for borrower/lender verification.

Quotes are valid for 60 seconds — borrower or lender must submit `originate()`
within that window or re-quote.
"""

from __future__ import annotations

import math
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from oracle.agent_sofr import compute_agent_sofr
from oracle.calibration import (
    BASE_ASSETS, BASE_CHAIN_ID,
    INTERAGENT_REPO_ADDRESS,
    EIP712_DOMAIN_NAME, EIP712_DOMAIN_VERSION,
    REGIME_MAX_LTV, LGD_DEFAULT, DEFAULT_MAX_DEFAULT_PROB,
    BAR_SECONDS, BARS_PER_YEAR,
)
from oracle.max_ltv import compute_math_max_ltv
from oracle.regime_classifier import RegimeClassifier
from oracle.variance_engine import (
    compute_variance_from_returns, fetch_live_eth_returns,
    VarianceSnapshot,
)
from scipy.stats import norm


# ─────────────────────────────────────────────────────────────────────────────
# Output type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignedQuote:
    """A loan quote signed by the Agent-SOFR oracle, ready for originate()."""

    # Quote fields (must match Solidity Quote struct exactly)
    borrower: str               # 0x… address
    lender: str
    principal_token: str        # ERC-20 address
    principal_amount: int       # uint256 raw units
    collateral_token: str
    collateral_amount: int
    expiry_timestamp: int
    rate_bps: int
    nonce: str                  # 0x… 32-byte hex

    # Signature
    signature: str              # 0x… 65-byte hex

    # Computed (for inspection)
    mode: Literal["compute_rate", "compute_collateral", "compute_max_duration"]
    ltv: float                  # principal_USD / collateral_USD
    sigma_T: float              # σ over horizon
    regime: str
    variance_premium_bps: float
    regime_premium_bps: float
    base_anchor_pct: float

    # Provenance
    methodology_version: str
    computed_at: int

    def to_dict(self) -> dict:
        return {
            "quote": {
                "borrower": self.borrower,
                "lender": self.lender,
                "principalToken": self.principal_token,
                "principalAmount": str(self.principal_amount),
                "collateralToken": self.collateral_token,
                "collateralAmount": str(self.collateral_amount),
                "expiryTimestamp": self.expiry_timestamp,
                "rateBps": self.rate_bps,
                "nonce": self.nonce,
            },
            "signature": self.signature,
            "decomposition": {
                "mode": self.mode,
                "ltv": round(self.ltv, 6),
                "sigma_T": round(self.sigma_T, 6),
                "regime": self.regime,
                "variance_premium_bps": round(self.variance_premium_bps, 3),
                "regime_premium_bps": round(self.regime_premium_bps, 3),
                "base_anchor_pct": round(self.base_anchor_pct, 4),
            },
            "contract": {
                "address": INTERAGENT_REPO_ADDRESS,
                "chain_id": BASE_CHAIN_ID,
            },
            "methodology_version": self.methodology_version,
            "computed_at": self.computed_at,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

QUOTE_VALIDITY_SECONDS: int = 60
ORCHESTRATOR_TAKE_BPS: float = 5.0       # 5 bps over fair rate (matcher fee)
DEFAULT_QUOTE_LTV: float = 0.80           # Used by compute_rate when LTV not specified


# ─────────────────────────────────────────────────────────────────────────────
# EIP-712 typed data
# ─────────────────────────────────────────────────────────────────────────────

def _eip712_quote_payload(quote_fields: dict) -> dict:
    """Build the EIP-712 typed-data payload that matches Solidity QUOTE_TYPEHASH."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Quote": [
                {"name": "borrower",          "type": "address"},
                {"name": "lender",            "type": "address"},
                {"name": "principalToken",    "type": "address"},
                {"name": "principalAmount",   "type": "uint256"},
                {"name": "collateralToken",   "type": "address"},
                {"name": "collateralAmount",  "type": "uint256"},
                {"name": "expiryTimestamp",   "type": "uint256"},
                {"name": "rateBps",           "type": "uint256"},
                {"name": "nonce",             "type": "bytes32"},
            ],
        },
        "primaryType": "Quote",
        "domain": {
            "name": EIP712_DOMAIN_NAME,
            "version": EIP712_DOMAIN_VERSION,
            "chainId": BASE_CHAIN_ID,
            "verifyingContract": INTERAGENT_REPO_ADDRESS,
        },
        "message": quote_fields,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main engine
# ─────────────────────────────────────────────────────────────────────────────

class QuoteEngine:
    """
    Stateful quote generator. Single global classifier instance for hysteresis.
    """

    def __init__(self, oracle_private_key: Optional[str] = None):
        """
        Args:
            oracle_private_key: hex string (0x… or raw) for the oracle signer.
                Defaults to ORACLE_PRIVATE_KEY env var.
        """
        pk = oracle_private_key or os.getenv("ORACLE_PRIVATE_KEY")
        if not pk:
            raise ValueError(
                "Oracle private key required. Set ORACLE_PRIVATE_KEY env var "
                "or pass to QuoteEngine(oracle_private_key=...)."
            )
        self._oracle = Account.from_key(pk)
        self._classifier = RegimeClassifier()

    @property
    def oracle_address(self) -> str:
        return self._oracle.address

    # ─── Public API ─────────────────────────────────────────────────────────

    def compute_rate(
        self,
        principal_amount_usd: float,    # in human units (e.g., 50.0 = \$50)
        collateral_amount_usd: float,   # in human units (USD value)
        duration_sec: int,
        borrower: str,
        lender: str,
        principal_asset: str = "USDC",
        collateral_asset: str = "WETH",
        collateral_price_usd: float = 2080.0,  # current spot
    ) -> SignedQuote:
        """
        Mode 1: given (principal, collateral, duration) → output (rate, signed quote).
        """
        variance, regime = self._fresh_state()
        ltv = principal_amount_usd / collateral_amount_usd
        if ltv > 1.0:
            raise ValueError(f"LTV > 1 not allowed (got {ltv:.3f}); collateral too small")

        sigma_T = variance.sigma_over_horizon(duration_sec)
        var_premium_bps = self._compute_variance_premium_bps(ltv, sigma_T, duration_sec)
        regime_premium_bps = self._regime_premium_bps(regime)

        # Fair rate (annualized %)
        sofr_snap = compute_agent_sofr("USD", duration_sec, ltv_for_premium=ltv)
        base_anchor = sofr_snap.base_anchor_pct
        rate_pct = base_anchor + var_premium_bps / 100 + regime_premium_bps / 100 + ORCHESTRATOR_TAKE_BPS / 100
        rate_bps = int(round(rate_pct * 100))  # 4.25% → 425 bps

        return self._sign_and_package(
            mode="compute_rate",
            principal_asset=principal_asset, principal_amount_usd=principal_amount_usd,
            collateral_asset=collateral_asset,
            collateral_amount_native=collateral_amount_usd / collateral_price_usd,
            duration_sec=duration_sec,
            rate_bps=rate_bps, ltv=ltv, sigma_T=sigma_T, regime=regime,
            var_premium_bps=var_premium_bps, regime_premium_bps=regime_premium_bps,
            base_anchor_pct=base_anchor,
            borrower=borrower, lender=lender,
        )

    def compute_collateral(
        self,
        principal_amount_usd: float,
        target_rate_bps: int,
        duration_sec: int,
        borrower: str,
        lender: str,
        principal_asset: str = "USDC",
        collateral_asset: str = "WETH",
        collateral_price_usd: float = 2080.0,
    ) -> SignedQuote:
        """
        Mode 2: given (principal, rate, duration) → output (required collateral, signed quote).
        Numerical inversion: find LTV such that quoted rate == target.
        """
        variance, regime = self._fresh_state()
        sigma_T = variance.sigma_over_horizon(duration_sec)
        regime_premium_bps = self._regime_premium_bps(regime)

        sofr_snap = compute_agent_sofr("USD", duration_sec, ltv_for_premium=0.80)
        base_anchor = sofr_snap.base_anchor_pct

        # Required variance_premium (bps) = target_rate - base - regime - take
        target_premium_bps = target_rate_bps - int(base_anchor * 100) - regime_premium_bps - ORCHESTRATOR_TAKE_BPS
        if target_premium_bps < 0:
            raise ValueError(
                f"Target rate {target_rate_bps} bps below floor "
                f"(base {base_anchor:.2f}% + regime {regime_premium_bps:.1f}bps + take {ORCHESTRATOR_TAKE_BPS:.1f}bps)"
            )

        # Numerical inversion: find LTV that produces target_premium
        ltv = self._invert_premium_to_ltv(target_premium_bps, sigma_T, duration_sec)
        # Cap at regime max
        ltv = min(ltv, REGIME_MAX_LTV[regime])

        collateral_amount_usd = principal_amount_usd / ltv
        collateral_amount_native = collateral_amount_usd / collateral_price_usd

        # Recompute variance premium at the final LTV (post-cap)
        var_premium_bps_actual = self._compute_variance_premium_bps(ltv, sigma_T, duration_sec)

        return self._sign_and_package(
            mode="compute_collateral",
            principal_asset=principal_asset, principal_amount_usd=principal_amount_usd,
            collateral_asset=collateral_asset,
            collateral_amount_native=collateral_amount_native,
            duration_sec=duration_sec,
            rate_bps=target_rate_bps, ltv=ltv, sigma_T=sigma_T, regime=regime,
            var_premium_bps=var_premium_bps_actual, regime_premium_bps=regime_premium_bps,
            base_anchor_pct=base_anchor,
            borrower=borrower, lender=lender,
        )

    def compute_max_duration(
        self,
        principal_amount_usd: float,
        collateral_amount_usd: float,
        target_rate_bps: int,
        borrower: str,
        lender: str,
        principal_asset: str = "USDC",
        collateral_asset: str = "WETH",
        collateral_price_usd: float = 2080.0,
    ) -> SignedQuote:
        """
        Mode 3: given (principal, collateral, rate) → output (max safe T, signed quote).
        """
        variance, regime = self._fresh_state()
        ltv = principal_amount_usd / collateral_amount_usd
        if ltv > REGIME_MAX_LTV[regime]:
            raise ValueError(
                f"LTV {ltv:.3f} exceeds regime cap {REGIME_MAX_LTV[regime]:.3f} for {regime}; "
                "increase collateral"
            )

        sofr_snap = compute_agent_sofr("USD", 3600, ltv_for_premium=ltv)
        base_anchor = sofr_snap.base_anchor_pct
        regime_premium_bps = self._regime_premium_bps(regime)

        target_premium_bps = target_rate_bps - int(base_anchor * 100) - regime_premium_bps - ORCHESTRATOR_TAKE_BPS
        if target_premium_bps < 0:
            raise ValueError(f"Target rate too low to clear premium")

        max_T_sec = self._invert_premium_to_duration(target_premium_bps, ltv, variance)
        # Snap to nearest standard bucket (down)
        for bucket in [86400, 14400, 3600, 1800, 300, 60]:
            if max_T_sec >= bucket:
                duration_sec = bucket
                break
        else:
            duration_sec = 60

        sigma_T = variance.sigma_over_horizon(duration_sec)
        var_premium_actual = self._compute_variance_premium_bps(ltv, sigma_T, duration_sec)

        return self._sign_and_package(
            mode="compute_max_duration",
            principal_asset=principal_asset, principal_amount_usd=principal_amount_usd,
            collateral_asset=collateral_asset,
            collateral_amount_native=collateral_amount_usd / collateral_price_usd,
            duration_sec=duration_sec,
            rate_bps=target_rate_bps, ltv=ltv, sigma_T=sigma_T, regime=regime,
            var_premium_bps=var_premium_actual, regime_premium_bps=regime_premium_bps,
            base_anchor_pct=base_anchor,
            borrower=borrower, lender=lender,
        )

    # ─── Internals ──────────────────────────────────────────────────────────

    def _fresh_state(self) -> tuple[VarianceSnapshot, str]:
        """Refresh variance + regime from live data."""
        returns, timestamps = fetch_live_eth_returns(n_bars=24)
        variance = compute_variance_from_returns(returns, timestamp=timestamps[-1])
        regime = self._classifier.classify(variance.sigma_5min).mode_name
        return variance, regime

    @staticmethod
    def _compute_variance_premium_bps(ltv: float, sigma_T: float, duration_sec: int) -> float:
        if sigma_T <= 0 or ltv <= 0 or ltv >= 1:
            return 0.0
        z = -math.log(1.0 / ltv) / sigma_T
        p_default = norm.cdf(z)
        bars = duration_sec / BAR_SECONDS
        expected_loss = ltv * p_default * LGD_DEFAULT
        # annualized bps
        return expected_loss * (BARS_PER_YEAR / bars) * 10_000

    @staticmethod
    def _regime_premium_bps(regime: str) -> float:
        from oracle.calibration import REGIME_PREMIUM_BPS
        return REGIME_PREMIUM_BPS[regime]

    def _invert_premium_to_ltv(
        self, target_premium_bps: float, sigma_T: float, duration_sec: int,
    ) -> float:
        """Bisection: find LTV such that variance_premium_bps(LTV, σ_T, T) == target."""
        lo, hi = 0.10, 0.99
        for _ in range(60):
            mid = (lo + hi) / 2
            prem = self._compute_variance_premium_bps(mid, sigma_T, duration_sec)
            if abs(prem - target_premium_bps) < 0.01:
                return mid
            if prem < target_premium_bps:
                lo = mid   # need higher LTV for higher premium
            else:
                hi = mid
        return mid

    def _invert_premium_to_duration(
        self, target_premium_bps: float, ltv: float, variance: VarianceSnapshot,
    ) -> int:
        """Bisection: find max duration such that var_premium ≤ target."""
        lo, hi = 60, 86400 * 7  # 1 min to 1 week
        for _ in range(40):
            mid = (lo + hi) // 2
            sigma_T = variance.sigma_over_horizon(mid)
            prem = self._compute_variance_premium_bps(ltv, sigma_T, mid)
            if prem > target_premium_bps:
                hi = mid - 1
            else:
                lo = mid + 1
        return lo

    def _sign_and_package(
        self,
        mode: Literal["compute_rate", "compute_collateral", "compute_max_duration"],
        principal_asset: str, principal_amount_usd: float,
        collateral_asset: str, collateral_amount_native: float,
        duration_sec: int, rate_bps: int, ltv: float, sigma_T: float, regime: str,
        var_premium_bps: float, regime_premium_bps: float, base_anchor_pct: float,
        borrower: str, lender: str,
    ) -> SignedQuote:
        """Common helper: build the EIP-712 message, sign, return SignedQuote."""
        p_meta = BASE_ASSETS[principal_asset]
        c_meta = BASE_ASSETS[collateral_asset]

        principal_raw = int(round(principal_amount_usd * (10 ** p_meta.decimals)))
        collateral_raw = int(round(collateral_amount_native * (10 ** c_meta.decimals)))

        now = int(time.time())
        expiry = now + duration_sec + 300  # 5-min buffer past loan duration

        nonce_bytes = secrets.token_bytes(32)
        nonce_hex = "0x" + nonce_bytes.hex()

        # Match Solidity Quote struct precisely
        quote_fields = {
            "borrower":          borrower,
            "lender":            lender,
            "principalToken":    p_meta.address,
            "principalAmount":   principal_raw,
            "collateralToken":   c_meta.address,
            "collateralAmount":  collateral_raw,
            "expiryTimestamp":   expiry,
            "rateBps":           rate_bps,
            "nonce":             nonce_hex,
        }

        payload = _eip712_quote_payload(quote_fields)
        signable = encode_typed_data(full_message=payload)
        sig = self._oracle.sign_message(signable)
        sig_hex = sig.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        return SignedQuote(
            borrower=borrower, lender=lender,
            principal_token=p_meta.address, principal_amount=principal_raw,
            collateral_token=c_meta.address, collateral_amount=collateral_raw,
            expiry_timestamp=expiry, rate_bps=rate_bps, nonce=nonce_hex,
            signature=sig_hex,
            mode=mode, ltv=ltv, sigma_T=sigma_T, regime=regime,
            variance_premium_bps=var_premium_bps,
            regime_premium_bps=regime_premium_bps,
            base_anchor_pct=base_anchor_pct,
            methodology_version="agent-sofr-v1",
            computed_at=now,
        )


__all__ = ["SignedQuote", "QuoteEngine"]
