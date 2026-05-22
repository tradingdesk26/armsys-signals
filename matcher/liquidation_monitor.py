"""
Liquidation monitor — scans active loans in the intent book and queries the
deployed V2 contract for their current LTV.

Anyone can call `InterAgentRepoV2.liquidate(loanId)` if the loan is liquidatable
(LTV ≥ 95% + grace period passed). Liquidator receives 3% bounty.

This module exposes the data agents need to MONITOR for opportunities — it
does NOT itself execute liquidations (that's the liquidator's gas + tx).
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional

from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import keccak

from oracle.calibration import (
    INTERAGENT_REPO_ADDRESS, BASE_CHAIN_ID,
    LIQUIDATION_LTV_BPS, LIQUIDATOR_BOUNTY_BPS, INSURANCE_FEE_BPS,
    CHAINLINK_ETH_USD_BASE,
)
# Alias for legacy code
INTERAGENT_REPO_V2_ADDRESS = INTERAGENT_REPO_ADDRESS
from matcher.intent_book import IntentBook


# ─────────────────────────────────────────────────────────────────────────────
# RPC config
# ─────────────────────────────────────────────────────────────────────────────

BASE_RPC = os.getenv(
    "BASE_RPC_URL",
    "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy",
)

# Function selector for currentLTV(bytes32):
# keccak256("currentLTV(bytes32)")[:4]
_SEL_CURRENT_LTV = keccak(b"currentLTV(bytes32)")[:4].hex()


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LoanLiquidationStatus:
    """On-chain LTV snapshot for one loan."""
    loan_id: str                # 0x… hex (the nonce)
    borrower: str
    lender: str
    principal_amount_raw: int   # USDC native units (6 decimals)
    collateral_amount_raw: int  # WETH native units (18 decimals)
    current_ltv_bps: int
    eth_price_e8: int
    liquidatable: bool
    estimated_bounty_native: int  # 3% of collateral_amount_raw

    def to_dict(self) -> dict:
        return {
            "loan_id": self.loan_id,
            "borrower": self.borrower,
            "lender": self.lender,
            "principal_amount_raw": str(self.principal_amount_raw),
            "principal_amount_usd": self.principal_amount_raw / 1e6,
            "collateral_amount_raw": str(self.collateral_amount_raw),
            "collateral_amount_eth": self.collateral_amount_raw / 1e18,
            "current_ltv_bps": self.current_ltv_bps,
            "current_ltv_pct": self.current_ltv_bps / 100,
            "eth_price_usd": self.eth_price_e8 / 1e8,
            "liquidatable": self.liquidatable,
            "estimated_bounty_eth": self.estimated_bounty_native / 1e18,
            "estimated_bounty_raw": str(self.estimated_bounty_native),
            "contract": INTERAGENT_REPO_V2_ADDRESS,
            "chain_id": BASE_CHAIN_ID,
        }


# ─────────────────────────────────────────────────────────────────────────────
# RPC helpers
# ─────────────────────────────────────────────────────────────────────────────

def _eth_call(to_address: str, data_hex: str) -> str:
    """Synchronous eth_call. Returns hex-encoded result (no 0x prefix)."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to_address, "data": data_hex}, "latest"],
    }
    req = urllib.request.Request(
        BASE_RPC,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
    if "error" in result:
        raise RuntimeError(f"eth_call failed: {result['error']}")
    return result["result"]


def fetch_loan_status(loan_id_hex: str) -> Optional[LoanLiquidationStatus]:
    """
    Query the V2 contract's currentLTV(loanId) view and return a parsed status.
    Returns None if the loan doesn't exist or is closed (LTV returns 0).
    """
    # Encode call data: selector + bytes32(loan_id)
    loan_id_bytes = bytes.fromhex(loan_id_hex[2:] if loan_id_hex.startswith("0x") else loan_id_hex)
    if len(loan_id_bytes) != 32:
        raise ValueError(f"loan_id must be 32 bytes, got {len(loan_id_bytes)}")

    call_data = "0x" + _SEL_CURRENT_LTV + loan_id_bytes.hex()
    result_hex = _eth_call(INTERAGENT_REPO_V2_ADDRESS, call_data)

    # Decode (uint256, uint256, bool)
    raw = bytes.fromhex(result_hex[2:] if result_hex.startswith("0x") else result_hex)
    if len(raw) < 96:
        return None
    ltv_bps, eth_price_e8, liquidatable = abi_decode(["uint256", "uint256", "bool"], raw)

    if ltv_bps == 0:
        return None  # loan closed or unsupported

    # We need the loan terms from intent book to populate the rest
    # — but this function only knows loan_id. Caller pairs it with intent data.
    return _StubStatus(
        loan_id=loan_id_hex,
        current_ltv_bps=ltv_bps,
        eth_price_e8=eth_price_e8,
        liquidatable=liquidatable,
    )


@dataclass(frozen=True)
class _StubStatus:
    """Intermediate result from fetch_loan_status — loan_id + on-chain LTV only."""
    loan_id: str
    current_ltv_bps: int
    eth_price_e8: int
    liquidatable: bool


# ─────────────────────────────────────────────────────────────────────────────
# Public API: scan all matched loans
# ─────────────────────────────────────────────────────────────────────────────

def scan_liquidatable_loans(book: IntentBook, limit: int = 100) -> list[LoanLiquidationStatus]:
    """
    Scan recent matches in the intent book, query on-chain status, return any
    that are currently liquidatable.
    """
    matches = book.recent_matches(limit=limit)

    out: list[LoanLiquidationStatus] = []
    for m in matches:
        try:
            quote_payload = json.loads(m["quote_payload"])
            qfields = quote_payload["quote"]
            loan_id = qfields["nonce"]
            stub = fetch_loan_status(loan_id)
            if stub is None:
                continue

            collateral_raw = int(qfields["collateralAmount"])
            bounty_raw = (collateral_raw * LIQUIDATOR_BOUNTY_BPS) // 10_000

            status = LoanLiquidationStatus(
                loan_id=loan_id,
                borrower=qfields["borrower"],
                lender=qfields["lender"],
                principal_amount_raw=int(qfields["principalAmount"]),
                collateral_amount_raw=collateral_raw,
                current_ltv_bps=stub.current_ltv_bps,
                eth_price_e8=stub.eth_price_e8,
                liquidatable=stub.liquidatable,
                estimated_bounty_native=bounty_raw,
            )
            out.append(status)
        except Exception:
            # Skip malformed / non-V2 matches
            continue

    return out


def scan_all_active_loans(book: IntentBook, limit: int = 100) -> list[LoanLiquidationStatus]:
    """
    Same as scan_liquidatable_loans but returns ALL active loans regardless
    of liquidation status — useful for dashboard / monitoring.
    """
    matches = book.recent_matches(limit=limit)
    out: list[LoanLiquidationStatus] = []
    for m in matches:
        try:
            quote_payload = json.loads(m["quote_payload"])
            qfields = quote_payload["quote"]
            loan_id = qfields["nonce"]
            stub = fetch_loan_status(loan_id)
            if stub is None:
                continue
            collateral_raw = int(qfields["collateralAmount"])
            bounty_raw = (collateral_raw * LIQUIDATOR_BOUNTY_BPS) // 10_000
            out.append(LoanLiquidationStatus(
                loan_id=loan_id,
                borrower=qfields["borrower"],
                lender=qfields["lender"],
                principal_amount_raw=int(qfields["principalAmount"]),
                collateral_amount_raw=collateral_raw,
                current_ltv_bps=stub.current_ltv_bps,
                eth_price_e8=stub.eth_price_e8,
                liquidatable=stub.liquidatable,
                estimated_bounty_native=bounty_raw,
            ))
        except Exception:
            continue
    return out


__all__ = [
    "LoanLiquidationStatus",
    "fetch_loan_status",
    "scan_liquidatable_loans",
    "scan_all_active_loans",
]
