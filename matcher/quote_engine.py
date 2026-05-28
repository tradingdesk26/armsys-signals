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

import json
import math
import os
import secrets
import time
import urllib.request
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

# Chainlink ETH/USD on Base — same feed the V4 contract reads. Fetching this on
# the matcher side (instead of using a hardcoded $2080 fallback) keeps LTV in
# the quote consistent with what the contract will compute at originate time.
CHAINLINK_ETH_USD_BASE: str = "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"
ETH_PRICE_CACHE_SECONDS: int = 25        # cache so each find_match doesn't spam RPC
# Conservative bias on the live price — matcher computes collateral using
# (1 - PRICE_SAFETY_BPS/10000) × Chainlink. This over-collateralizes by a
# small margin so a 0.5% price drop between matcher tick and originate tick
# doesn't break LTV cap (initial LTV cap = LIQUIDATION_LTV - 200bps buffer = 93%).
PRICE_SAFETY_BPS: int = 50               # 0.50% conservative discount
# Hard fallback if Chainlink RPC fails. Better to refuse than to use a wildly
# stale value — but for stability we still pin a sane default.
ETH_PRICE_FALLBACK_USD: float = 2000.0


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
        self._eth_price_cache: tuple[float, float] = (0.0, 0.0)  # (price, fetched_at)

    @property
    def oracle_address(self) -> str:
        return self._oracle.address

    # ─── Chainlink ETH/USD ──────────────────────────────────────────────────

    def _fetch_live_eth_usd(self) -> float:
        """Read ETH/USD from the same Chainlink feed the V4 contract uses.

        Cached for ETH_PRICE_CACHE_SECONDS so a burst of find_match cycles
        doesn't hammer the RPC. If the call fails, falls through to a sane
        default (logged via stderr) — the matcher continues operating but
        with stale price, accepting the risk that some quotes might be
        rejected on-chain via InitialLtvTooHigh.
        """
        now = time.time()
        cached_price, cached_at = self._eth_price_cache
        if now - cached_at < ETH_PRICE_CACHE_SECONDS and cached_price > 0:
            return cached_price

        rpc = os.getenv("RPC_URL", "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy")
        try:
            req = urllib.request.Request(
                rpc,
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "eth_call",
                    "params": [{"to": CHAINLINK_ETH_USD_BASE, "data": "0xfeaf968c"}, "latest"],
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
            data = body["result"][2:]  # strip 0x
            # latestRoundData(): (roundId, answer, startedAt, updatedAt, answeredInRound)
            # Each field is 32 bytes (64 hex). `answer` is int256 at offset 64..128.
            answer = int(data[64:128], 16)
            price = answer / 1e8  # Chainlink ETH/USD has 8 decimals
            if not (500 < price < 100000):
                raise ValueError(f"sanity check failed: {price=}")
            # Apply conservative safety discount so we slightly over-collateralize
            safe_price = price * (1.0 - PRICE_SAFETY_BPS / 10_000)
            self._eth_price_cache = (safe_price, now)
            return safe_price
        except Exception as e:
            import sys
            print(f"[QuoteEngine] Chainlink ETH/USD fetch failed: {type(e).__name__}: {e}; "
                  f"using fallback ${ETH_PRICE_FALLBACK_USD}", file=sys.stderr, flush=True)
            self._eth_price_cache = (ETH_PRICE_FALLBACK_USD, now)
            return ETH_PRICE_FALLBACK_USD

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
        collateral_price_usd: Optional[float] = None,  # None → live Chainlink
    ) -> SignedQuote:
        """
        Mode 1: given (principal, collateral, duration) → output (rate, signed quote).
        """
        if collateral_price_usd is None:
            collateral_price_usd = self._fetch_live_eth_usd()
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
        collateral_price_usd: Optional[float] = None,  # None → live Chainlink
    ) -> SignedQuote:
        """
        Mode 2: given (principal, rate, duration) → output (required collateral, signed quote).
        Numerical inversion: find LTV such that quoted rate == target.
        """
        if collateral_price_usd is None:
            collateral_price_usd = self._fetch_live_eth_usd()
        variance, regime = self._fresh_state()
        sigma_T = variance.sigma_over_horizon(duration_sec)
        regime_premium_bps = self._regime_premium_bps(regime)

        sofr_snap = compute_agent_sofr("USD", duration_sec, ltv_for_premium=0.80)
        base_anchor = sofr_snap.base_anchor_pct

        # Required variance_premium (bps) = target_rate - base - regime - take
        floor_bps = int(round(base_anchor * 100)) + int(round(regime_premium_bps)) + int(round(ORCHESTRATOR_TAKE_BPS))
        target_premium_bps = target_rate_bps - int(base_anchor * 100) - regime_premium_bps - ORCHESTRATOR_TAKE_BPS

        # Floor-clamp: if lender posted at a rate below the SOFR+regime+take floor
        # (e.g. they wanted 480bps but regime moved up and floor is now 544bps),
        # we DON'T refuse — we bump the clearing rate up to the floor. Semantically
        # the lender intent is "≥min_rate", so getting MORE than they asked for is
        # consistent. The borrower still pays ≤ their max_rate (matcher checks that
        # at the pair-compat step), and the loan is now properly compensated for
        # default-risk under current regime.
        rate_bumped_from_bps: Optional[int] = None
        if target_premium_bps < 0:
            rate_bumped_from_bps = target_rate_bps
            target_rate_bps = floor_bps
            target_premium_bps = 0.0  # exactly at floor — variance premium is zero
            # We'll let the borrower's max_rate check downstream guard against
            # bumps that exceed what the borrower will accept (in matcher.py).

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
        collateral_price_usd: Optional[float] = None,  # None → live Chainlink
    ) -> SignedQuote:
        """
        Mode 3: given (principal, collateral, rate) → output (max safe T, signed quote).
        """
        if collateral_price_usd is None:
            collateral_price_usd = self._fetch_live_eth_usd()
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
