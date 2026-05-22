"""
Loan history — reconstructs the full lifecycle of every V4 loan from on-chain
event logs.

For each `LoanOriginated` event we fetch the matching `LoanRepaid`,
`LoanDefaulted`, or `LoanLiquidated` event (if any) to determine the loan's
final status. The result is a flat list of records suitable for the public
loan registry on the landing page.

We rely on indexed `bytes32 loanId` topics for cross-event joins; data fields
are ABI-decoded with eth_abi.

Scan window: last `LOOKBACK_BLOCKS` blocks (~14h on Base @ 2s/block). Adjust
if backfill is needed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from eth_abi import decode as abi_decode
from eth_utils import keccak

from oracle.calibration import INTERAGENT_REPO_V4_ADDRESS, BASE_CHAIN_ID

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

BASE_RPC = os.getenv(
    "BASE_RPC_URL",
    "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy",
)

# How far back to scan for originate events.
# Base @ 2s/block → 300k blocks ≈ 7 days. Covers the full Agora submission
# window so the demo loan stays visible.
# V4 is a low-activity contract so each batched call returns near-instantly
# even with large ranges; we batch in 50k-block windows.
LOOKBACK_BLOCKS: int = 300_000
BATCH_BLOCKS:    int = 50_000

# Event topic[0] = keccak256(signature)
TOPIC_ORIGINATED  = "0x" + keccak(text="LoanOriginated(bytes32,address,address,address,uint256,address,uint256,uint256,uint256,uint256,uint256)").hex()
TOPIC_REPAID      = "0x" + keccak(text="LoanRepaid(bytes32,uint256,uint256,uint256)").hex()
TOPIC_DEFAULTED   = "0x" + keccak(text="LoanDefaulted(bytes32,address,uint256,uint256,uint256,uint256,uint256)").hex()
TOPIC_LIQUIDATED  = "0x" + keccak(text="LoanLiquidated(bytes32,address,uint256,uint256,uint256,uint256,uint256)").hex()


# ─────────────────────────────────────────────────────────────────────────────
# RPC helper (synchronous, urllib only — no extra deps)
# ─────────────────────────────────────────────────────────────────────────────

def _rpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        BASE_RPC,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    if "error" in result:
        raise RuntimeError(f"{method} failed: {result['error']}")
    return result["result"]


def _get_block_number() -> int:
    return int(_rpc("eth_blockNumber", []), 16)


def _get_block_timestamp(block_number: int) -> int:
    block = _rpc("eth_getBlockByNumber", [hex(block_number), False])
    return int(block["timestamp"], 16)


def _get_logs(from_block: int, to_block: int, topic0: str) -> list[dict]:
    """Fetch logs from V4 with a given topic0 in the block range (inclusive)."""
    return _rpc("eth_getLogs", [{
        "address": INTERAGENT_REPO_V4_ADDRESS,
        "topics": [topic0],
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
    }])


def _get_logs_batched(from_block: int, to_block: int, topic0: str) -> list[dict]:
    """Same as _get_logs, but splits the range into BATCH_BLOCKS chunks to stay under provider limits."""
    out: list[dict] = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + BATCH_BLOCKS - 1, to_block)
        out.extend(_get_logs(cur, end, topic0))
        cur = end + 1
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Event decoders
# ─────────────────────────────────────────────────────────────────────────────

def _decode_originated(log: dict) -> dict:
    """Decode LoanOriginated event. topic[1..3]=loanId/borrower/lender, data=rest."""
    topics = log["topics"]
    data_bytes = bytes.fromhex(log["data"][2:])
    # data fields (non-indexed):
    # principalToken, principalAmount, collateralToken, collateralAmount,
    # expiryTimestamp, rateBps, initialLtvBps, ethPriceE8AtOrigination
    decoded = abi_decode(
        ["address", "uint256", "address", "uint256", "uint256", "uint256", "uint256", "uint256"],
        data_bytes,
    )
    return {
        "loan_id":             topics[1],                               # 0x-prefixed 32-byte hex
        "borrower":            "0x" + topics[2][-40:],
        "lender":              "0x" + topics[3][-40:],
        "principal_token":     decoded[0],
        "principal_amount":    decoded[1],
        "collateral_token":    decoded[2],
        "collateral_amount":   decoded[3],
        "expiry_timestamp":    decoded[4],
        "rate_bps":            decoded[5],
        "initial_ltv_bps":     decoded[6],
        "eth_price_e8_origin": decoded[7],
        "tx_hash":             log["transactionHash"],
        "block_number":        int(log["blockNumber"], 16),
        "log_index":           int(log["logIndex"], 16),
    }


def _decode_close_event(log: dict, kind: str) -> dict:
    """Decode any of LoanRepaid / LoanDefaulted / LoanLiquidated."""
    topics = log["topics"]
    data_bytes = bytes.fromhex(log["data"][2:])
    out = {
        "loan_id":      topics[1],
        "kind":         kind,                                 # "repaid" | "defaulted" | "liquidated"
        "tx_hash":      log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
    }
    if kind == "repaid":
        # LoanRepaid(bytes32 indexed loanId, uint256 principalRepaid, uint256 interestPaid, uint256 collateralReleased)
        d = abi_decode(["uint256", "uint256", "uint256"], data_bytes)
        out["principal_repaid"]    = d[0]
        out["interest_paid"]       = d[1]
        out["collateral_released"] = d[2]
    elif kind == "defaulted":
        # LoanDefaulted(bytes32 indexed loanId, address indexed triggerer, uint256 ethPriceE8, uint256 bountyPaid, uint256 insuranceFee, uint256 lenderRecovered, uint256 borrowerRefund)
        d = abi_decode(["uint256", "uint256", "uint256", "uint256", "uint256"], data_bytes)
        out["triggerer"]        = "0x" + topics[2][-40:]
        out["eth_price_e8"]     = d[0]
        out["bounty_paid"]      = d[1]
        out["insurance_fee"]    = d[2]
        out["lender_recovered"] = d[3]
        out["borrower_refund"]  = d[4]
    elif kind == "liquidated":
        # LoanLiquidated(bytes32 indexed loanId, address indexed liquidator, uint256 currentLtvBps, uint256 ethPriceE8, uint256 bountyPaid, uint256 insuranceFee, uint256 lenderRecovered)
        d = abi_decode(["uint256", "uint256", "uint256", "uint256", "uint256"], data_bytes)
        out["liquidator"]       = "0x" + topics[2][-40:]
        out["current_ltv_bps"]  = d[0]
        out["eth_price_e8"]     = d[1]
        out["bounty_paid"]      = d[2]
        out["insurance_fee"]    = d[3]
        out["lender_recovered"] = d[4]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Cache — re-scanning every request is expensive (~5 RPC calls per call).
# Cache the registry for 30s; landing polls at 30s anyway.
# ─────────────────────────────────────────────────────────────────────────────

_REGISTRY_CACHE: dict = {"ts": 0.0, "data": []}
_CACHE_TTL_SEC: float = 30.0


def _block_timestamp_cache() -> dict[int, int]:
    """Per-call cache for block timestamps (avoids redundant RPC during one scan)."""
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_loan_registry(limit: int = 50) -> list[dict]:
    """
    Returns the last `limit` loans (originated within LOOKBACK_BLOCKS), each
    enriched with its close-event tx hash + final status.

    Cached for _CACHE_TTL_SEC seconds.
    """
    now = time.time()
    if now - _REGISTRY_CACHE["ts"] < _CACHE_TTL_SEC and _REGISTRY_CACHE["data"]:
        return _REGISTRY_CACHE["data"][:limit]

    latest = _get_block_number()
    from_block = max(0, latest - LOOKBACK_BLOCKS)

    # Step 1: pull all LoanOriginated in window
    origin_logs = _get_logs_batched(from_block, latest, TOPIC_ORIGINATED)
    originated = [_decode_originated(l) for l in origin_logs]
    originated.sort(key=lambda x: (x["block_number"], x["log_index"]), reverse=True)
    originated = originated[:limit]

    if not originated:
        _REGISTRY_CACHE.update({"ts": now, "data": []})
        return []

    # Step 2: pull close events in same window (cheap — 3 more eth_getLogs)
    repaid_logs     = _get_logs_batched(from_block, latest, TOPIC_REPAID)
    defaulted_logs  = _get_logs_batched(from_block, latest, TOPIC_DEFAULTED)
    liquidated_logs = _get_logs_batched(from_block, latest, TOPIC_LIQUIDATED)

    close_by_id: dict[str, dict] = {}
    for log in repaid_logs:
        d = _decode_close_event(log, "repaid")
        close_by_id[d["loan_id"].lower()] = d
    for log in defaulted_logs:
        d = _decode_close_event(log, "defaulted")
        close_by_id[d["loan_id"].lower()] = d
    for log in liquidated_logs:
        d = _decode_close_event(log, "liquidated")
        close_by_id[d["loan_id"].lower()] = d

    # Step 3: assemble + add block timestamps (one RPC per unique block)
    ts_cache: dict[int, int] = {}
    def _ts(bn: int) -> int:
        if bn not in ts_cache:
            ts_cache[bn] = _get_block_timestamp(bn)
        return ts_cache[bn]

    out: list[dict] = []
    for orig in originated:
        close = close_by_id.get(orig["loan_id"].lower())
        record = {
            "loan_id":             orig["loan_id"],
            "status":              close["kind"] if close else "active",
            "borrower":            orig["borrower"],
            "lender":              orig["lender"],
            "principal_token":     orig["principal_token"],
            "principal_amount":    str(orig["principal_amount"]),
            "principal_amount_usdc": orig["principal_amount"] / 1e6,
            "collateral_token":    orig["collateral_token"],
            "collateral_amount":   str(orig["collateral_amount"]),
            "collateral_amount_weth": orig["collateral_amount"] / 1e18,
            "expiry_timestamp":    orig["expiry_timestamp"],
            "duration_sec":        orig["expiry_timestamp"] - _ts(orig["block_number"]),
            "rate_bps":            orig["rate_bps"],
            "rate_pct":            orig["rate_bps"] / 100,
            "initial_ltv_bps":     orig["initial_ltv_bps"],
            "initial_ltv_pct":     orig["initial_ltv_bps"] / 100,
            "eth_price_origin":    orig["eth_price_e8_origin"] / 1e8,
            "originate_tx":        orig["tx_hash"],
            "originate_block":     orig["block_number"],
            "originate_timestamp": _ts(orig["block_number"]),
            "close_tx":            close["tx_hash"] if close else None,
            "close_block":         close["block_number"] if close else None,
            "close_timestamp":     _ts(close["block_number"]) if close else None,
            "contract":            INTERAGENT_REPO_V4_ADDRESS,
            "chain_id":            BASE_CHAIN_ID,
        }
        if close and close["kind"] == "repaid":
            record["interest_paid_usdc"] = close["interest_paid"] / 1e6
        elif close and close["kind"] == "liquidated":
            record["liquidator"]      = close.get("liquidator")
            record["close_ltv_bps"]   = close.get("current_ltv_bps")
        elif close and close["kind"] == "defaulted":
            record["triggerer"]       = close.get("triggerer")
        out.append(record)

    _REGISTRY_CACHE.update({"ts": now, "data": out})
    return out


__all__ = ["build_loan_registry"]
