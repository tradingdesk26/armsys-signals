"""
Multi-source rate aggregator for Agent-SOFR.

Fetches rates from 8 sources, applies pre-defined weights, returns
the weighted median as the fair base anchor.

Sources:
    Market-derived (70%):
        - deribit_pcp_30d         — ETH options put-call parity, 30d expiry
        - hl_funding_smoothed     — Hyperliquid ETH-PERP funding (annualized)
        - aevo_pcp                — Aevo options PCP
        - deribit_basis_3m        — Deribit ETH futures basis, 3m expiry
    Reference (20%):
        - aave_borrow_usdc        — Aave V3 Base USDC borrow rate
        - compound_borrow_usdc    — Compound Base USDC borrow rate (TODO)
        - aave_borrow_weth        — Aave V3 Base WETH borrow rate
    Macro anchor (10%):
        - sofr_30d                — NY Fed SOFR 30-day average (fallback constant)

All rates returned as annualized %. None on fetch failure → weight redistributed.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from oracle.calibration import (
    SOURCE_WEIGHTS, ETH_STAKING_YIELD_PCT_DEFAULT,
    CACHE_TTL_SEC, RATE_FLOOR_PCT, RATE_CEILING_PCT,
)


# ─────────────────────────────────────────────────────────────────────────────
# Source result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RateSource:
    """Single source's contribution to the rate aggregator."""
    name: str
    rate_pct: Optional[float]   # annualized % USD rate (None on fetch failure)
    weight: float               # configured weight (from SOURCE_WEIGHTS)
    timestamp: int              # when this rate was sampled
    raw_data: dict = field(default_factory=dict)  # debug context
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.rate_pct is not None and self.error is None


@dataclass(frozen=True)
class AggregatedRate:
    """Result of running the aggregator across all sources."""
    rate_pct: float                          # weighted median (annualized %)
    sources: dict[str, RateSource]           # per-source breakdown
    effective_weight_sum: float              # sum of weights after dropping None
    timestamp: int


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper — keep it simple, no async for Day 1
# ─────────────────────────────────────────────────────────────────────────────

def _http_get_json(url: str, timeout: float = 5.0) -> dict:
    """Synchronous GET → JSON. Raises on error."""
    req = urllib.request.Request(url, headers={"User-Agent": "agent-sofr/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, payload: dict, timeout: float = 5.0) -> dict:
    """Synchronous POST → JSON. Raises on error."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "agent-sofr/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Source 1: Deribit ETH options put-call parity (30-day ATM)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_deribit_pcp_30d(
    eth_staking_yield_pct: float = ETH_STAKING_YIELD_PCT_DEFAULT,
) -> RateSource:
    """
    Extract implied USD short rate from ETH options PCP.

    PCP: C - P = S - K*e^(-r*T)
    Solve: r = -ln((S - C + P) / K) / T
    Then: r_USD = r_implied + q_ETH (add back staking yield)
    """
    name = "deribit_pcp_30d"
    weight = SOURCE_WEIGHTS[name]
    ts = int(time.time())

    try:
        # Get ETH spot
        spot_data = _http_get_json(
            "https://www.deribit.com/api/v2/public/get_index_price?index_name=eth_usd"
        )
        spot = float(spot_data["result"]["index_price"])

        # Get all options, pick one near ATM with ~30d expiry
        inst_data = _http_get_json(
            "https://www.deribit.com/api/v2/public/get_instruments?"
            "currency=ETH&kind=option&expired=false"
        )
        now_ms = ts * 1000
        target_T_days = 30
        target_ms = now_ms + target_T_days * 24 * 3600 * 1000

        # Find expiries closest to 30 days
        expiries = {}
        for inst in inst_data["result"]:
            exp = inst.get("expiration_timestamp")
            if exp and exp > now_ms:
                key = exp
                expiries.setdefault(key, []).append(inst)

        if not expiries:
            return RateSource(name, None, weight, ts, error="no expiries")

        # Closest expiry to target
        best_exp = min(expiries.keys(), key=lambda e: abs(e - target_ms))
        exp_insts = expiries[best_exp]

        # ATM strike — closest to spot
        strikes = sorted({float(i["strike"]) for i in exp_insts if "strike" in i})
        if not strikes:
            return RateSource(name, None, weight, ts, error="no strikes")
        K = min(strikes, key=lambda s: abs(s - spot))

        # Get call and put marks
        c_name = next((i["instrument_name"] for i in exp_insts
                       if i.get("strike") == K and i["instrument_name"].endswith("-C")), None)
        p_name = next((i["instrument_name"] for i in exp_insts
                       if i.get("strike") == K and i["instrument_name"].endswith("-P")), None)
        if not c_name or not p_name:
            return RateSource(name, None, weight, ts, error="missing C/P instrument")

        c_tick = _http_get_json(
            f"https://www.deribit.com/api/v2/public/ticker?instrument_name={c_name}"
        )
        p_tick = _http_get_json(
            f"https://www.deribit.com/api/v2/public/ticker?instrument_name={p_name}"
        )

        c_mark_eth = float(c_tick["result"]["mark_price"])
        p_mark_eth = float(p_tick["result"]["mark_price"])
        C_usd = c_mark_eth * spot
        P_usd = p_mark_eth * spot

        T_years = (best_exp - now_ms) / (365 * 24 * 3600 * 1000)
        if T_years <= 0:
            return RateSource(name, None, weight, ts, error="non-positive T")

        # PCP solve
        x = (spot - C_usd + P_usd) / K
        if x <= 0:
            return RateSource(name, None, weight, ts, error=f"PCP arg negative: {x}")
        r_implied = -math.log(x) / T_years * 100  # %

        # Add staking yield to get pure USD rate
        r_usd = r_implied + eth_staking_yield_pct

        return RateSource(
            name=name, rate_pct=r_usd, weight=weight, timestamp=ts,
            raw_data={
                "spot": spot, "strike": K, "T_days": T_years * 365,
                "C_usd": C_usd, "P_usd": P_usd,
                "r_implied": r_implied, "staking_yield": eth_staking_yield_pct,
            },
        )
    except Exception as e:
        return RateSource(name, None, weight, ts, error=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Source 2: Hyperliquid ETH-PERP funding rate
# ─────────────────────────────────────────────────────────────────────────────

def fetch_hl_funding_smoothed() -> RateSource:
    """
    Annualized HL ETH-PERP funding rate (1h rate × 8760).
    Reflects USD-cost-of-leverage demand on the largest perp venue.
    """
    name = "hl_funding_smoothed"
    weight = SOURCE_WEIGHTS[name]
    ts = int(time.time())

    try:
        data = _http_post_json(
            "https://api.hyperliquid.xyz/info",
            payload={"type": "metaAndAssetCtxs"},
        )
        meta, ctxs = data
        eth_idx = next(
            i for i, u in enumerate(meta["universe"]) if u["name"] == "ETH"
        )
        eth_ctx = ctxs[eth_idx]
        funding_1h = float(eth_ctx["funding"])  # per-hour rate as decimal
        funding_annualized_pct = funding_1h * 24 * 365 * 100

        return RateSource(
            name=name, rate_pct=funding_annualized_pct, weight=weight,
            timestamp=ts,
            raw_data={"funding_1h": funding_1h, "mark_px": eth_ctx.get("markPx")},
        )
    except Exception as e:
        return RateSource(name, None, weight, ts, error=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Source 3: Aevo ETH options PCP
# ─────────────────────────────────────────────────────────────────────────────

def fetch_aevo_pcp(
    eth_staking_yield_pct: float = ETH_STAKING_YIELD_PCT_DEFAULT,
) -> RateSource:
    """ETH options PCP via Aevo orderbook (similar logic to Deribit)."""
    name = "aevo_pcp"
    weight = SOURCE_WEIGHTS[name]
    ts = int(time.time())

    try:
        spot_data = _http_get_json("https://api.aevo.xyz/index?asset=ETH")
        spot = float(spot_data["price"])

        # Pull markets, find ETH options near ATM ~30d out
        markets = _http_get_json("https://api.aevo.xyz/markets")
        now_s = ts
        target_T_days = 30
        target_s = now_s + target_T_days * 24 * 3600

        options = [
            m for m in markets
            if m.get("underlying_asset") == "ETH"
            and m.get("instrument_type") == "OPTION"
            and "expiry" in m
        ]
        if not options:
            return RateSource(name, None, weight, ts, error="no ETH options listed")

        # Group by expiry
        by_expiry: dict[int, list[dict]] = {}
        for m in options:
            exp = int(m["expiry"])  # already epoch seconds on Aevo
            if exp > now_s:
                by_expiry.setdefault(exp, []).append(m)

        if not by_expiry:
            return RateSource(name, None, weight, ts, error="no future expiries")

        best_exp = min(by_expiry.keys(), key=lambda e: abs(e - target_s))
        exp_insts = by_expiry[best_exp]
        strikes = sorted({float(m["strike"]) for m in exp_insts if "strike" in m})
        if not strikes:
            return RateSource(name, None, weight, ts, error="no strikes")
        K = min(strikes, key=lambda s: abs(s - spot))

        c = next((m["instrument_name"] for m in exp_insts
                  if float(m.get("strike", -1)) == K and m["instrument_name"].endswith("-C")), None)
        p = next((m["instrument_name"] for m in exp_insts
                  if float(m.get("strike", -1)) == K and m["instrument_name"].endswith("-P")), None)
        if not c or not p:
            return RateSource(name, None, weight, ts, error="missing C/P")

        c_ob = _http_get_json(f"https://api.aevo.xyz/orderbook?instrument_name={c}")
        p_ob = _http_get_json(f"https://api.aevo.xyz/orderbook?instrument_name={p}")

        def mid(ob: dict) -> Optional[float]:
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            b = float(bids[0][0]) if bids else None
            a = float(asks[0][0]) if asks else None
            if b and a:
                return (a + b) / 2
            return b or a

        C_usd = mid(c_ob)
        P_usd = mid(p_ob)
        if C_usd is None or P_usd is None:
            return RateSource(name, None, weight, ts, error="empty orderbook")

        T_years = (best_exp - now_s) / (365 * 24 * 3600)
        if T_years <= 0:
            return RateSource(name, None, weight, ts, error="non-positive T")

        x = (spot - C_usd + P_usd) / K
        if x <= 0:
            return RateSource(name, None, weight, ts, error="PCP arg negative")
        r_implied = -math.log(x) / T_years * 100
        r_usd = r_implied + eth_staking_yield_pct

        return RateSource(
            name=name, rate_pct=r_usd, weight=weight, timestamp=ts,
            raw_data={
                "spot": spot, "strike": K, "T_days": T_years * 365,
                "C_usd": C_usd, "P_usd": P_usd,
                "r_implied": r_implied,
            },
        )
    except Exception as e:
        return RateSource(name, None, weight, ts, error=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Source 4: Deribit ETH futures basis (3m expiry)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_deribit_basis_3m(
    eth_staking_yield_pct: float = ETH_STAKING_YIELD_PCT_DEFAULT,
) -> RateSource:
    """ETH futures basis r_implied = ln(F/S)/T; r_USD = r_implied + q_ETH."""
    name = "deribit_basis_3m"
    weight = SOURCE_WEIGHTS[name]
    ts = int(time.time())

    try:
        spot_data = _http_get_json(
            "https://www.deribit.com/api/v2/public/get_index_price?index_name=eth_usd"
        )
        spot = float(spot_data["result"]["index_price"])

        inst_data = _http_get_json(
            "https://www.deribit.com/api/v2/public/get_instruments?"
            "currency=ETH&kind=future&expired=false"
        )
        now_ms = ts * 1000
        target_ms = now_ms + 90 * 24 * 3600 * 1000

        # Closest expiry to 90 days, excluding perpetual
        candidates = [
            i for i in inst_data["result"]
            if i.get("expiration_timestamp")
            and i["instrument_name"] != "ETH-PERPETUAL"
            and i["expiration_timestamp"] > now_ms
        ]
        if not candidates:
            return RateSource(name, None, weight, ts, error="no future contracts")

        best = min(candidates, key=lambda i: abs(i["expiration_timestamp"] - target_ms))
        tick = _http_get_json(
            f"https://www.deribit.com/api/v2/public/ticker?"
            f"instrument_name={best['instrument_name']}"
        )
        F = float(tick["result"]["mark_price"])
        T_years = (best["expiration_timestamp"] - now_ms) / (365 * 24 * 3600 * 1000)
        if T_years <= 0 or F <= 0:
            return RateSource(name, None, weight, ts, error="invalid F/T")

        r_implied = math.log(F / spot) / T_years * 100
        r_usd = r_implied + eth_staking_yield_pct

        return RateSource(
            name=name, rate_pct=r_usd, weight=weight, timestamp=ts,
            raw_data={
                "spot": spot, "F": F, "T_days": T_years * 365,
                "instrument": best["instrument_name"], "r_implied": r_implied,
            },
        )
    except Exception as e:
        return RateSource(name, None, weight, ts, error=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Source 5 & 7: Aave V3 Base lending rates (USDC + WETH)
# ─────────────────────────────────────────────────────────────────────────────

# Aave V3 Pool on Base
_AAVE_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
_BASE_RPC = "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy"
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_WETH_BASE = "0x4200000000000000000000000000000000000006"
# getReserveData selector
_GET_RESERVE_DATA_SEL = "0x35ea6a75"


def _fetch_aave_borrow_rate(token_addr: str, source_name: str) -> RateSource:
    """Read currentVariableBorrowRate from Aave V3 reserve struct on Base."""
    weight = SOURCE_WEIGHTS[source_name]
    ts = int(time.time())

    try:
        padded = token_addr[2:].lower().rjust(64, "0")
        data = _GET_RESERVE_DATA_SEL + padded
        resp = _http_post_json(_BASE_RPC, {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": _AAVE_POOL, "data": data}, "latest"],
        })
        if "error" in resp:
            return RateSource(source_name, None, weight, ts,
                              error=f"rpc error: {resp['error']}")

        hex_data = resp["result"][2:]  # strip 0x
        # Layout (each 32 bytes):
        # [0] configuration, [1] liquidityIndex, [2] currentLiquidityRate,
        # [3] variableBorrowIndex, [4] currentVariableBorrowRate, ...
        var_borrow_ray = int(hex_data[4*64:5*64], 16)
        rate_pct = var_borrow_ray / 1e27 * 100

        return RateSource(
            name=source_name, rate_pct=rate_pct, weight=weight, timestamp=ts,
            raw_data={"borrow_ray": var_borrow_ray},
        )
    except Exception as e:
        return RateSource(source_name, None, weight, ts, error=str(e))


def fetch_aave_borrow_usdc() -> RateSource:
    return _fetch_aave_borrow_rate(_USDC_BASE, "aave_borrow_usdc")


def fetch_aave_borrow_weth() -> RateSource:
    return _fetch_aave_borrow_rate(_WETH_BASE, "aave_borrow_weth")


# ─────────────────────────────────────────────────────────────────────────────
# Source 6: Compound V3 Base USDC — TODO: implement cUSDCv3 borrow rate read
# ─────────────────────────────────────────────────────────────────────────────

def fetch_compound_borrow_usdc() -> RateSource:
    """
    TODO: read getBorrowRate from cUSDCv3 on Base.
    For now: returns None so weight redistributes to other sources.
    """
    name = "compound_borrow_usdc"
    weight = SOURCE_WEIGHTS[name]
    ts = int(time.time())
    return RateSource(name, None, weight, ts, error="not_implemented_yet")


# ─────────────────────────────────────────────────────────────────────────────
# Source 8: SOFR 30-day reference (macro anchor)
# ─────────────────────────────────────────────────────────────────────────────

# Slow-moving macro rate. Hardcoded fallback updated manually; ideally
# polled from NY Fed API hourly.
SOFR_30D_FALLBACK: float = 4.32  # % as of May 2026


def fetch_sofr_30d() -> RateSource:
    """SOFR 30-day average. Currently returns hardcoded fallback."""
    name = "sofr_30d"
    weight = SOURCE_WEIGHTS[name]
    ts = int(time.time())
    return RateSource(
        name=name, rate_pct=SOFR_30D_FALLBACK, weight=weight, timestamp=ts,
        raw_data={"source": "hardcoded_fallback_v1"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Weighted median (renormalizes for missing sources)
# ─────────────────────────────────────────────────────────────────────────────

def weighted_median_value(values: list[tuple[float, float]]) -> float:
    """
    Weighted median of (value, weight) pairs.
    Renormalizes weights to sum to 1 first.
    """
    if not values:
        raise ValueError("no values to median")

    total_w = sum(w for _, w in values)
    if total_w <= 0:
        raise ValueError("weights sum to zero")
    pairs = sorted([(v, w / total_w) for v, w in values], key=lambda p: p[0])

    cum = 0.0
    for v, w in pairs:
        cum += w
        if cum >= 0.5:
            return v
    return pairs[-1][0]  # shouldn't reach unless float drift


# ─────────────────────────────────────────────────────────────────────────────
# Top-level aggregator
# ─────────────────────────────────────────────────────────────────────────────

# Module-level cache: {(asset,): (timestamp, AggregatedRate)}
_RATE_CACHE: dict[tuple, tuple[int, AggregatedRate]] = {}


def aggregate_rates(
    asset: str = "USD",
    use_cache: bool = True,
    eth_staking_yield_pct: float = ETH_STAKING_YIELD_PCT_DEFAULT,
) -> AggregatedRate:
    """
    Fetch all sources, compute weighted median.

    Args:
        asset: which asset's rate we want (USD/EUR/ETH).
               Day 1 supports USD only — other assets to be added.
        use_cache: return cached result if within CACHE_TTL_SEC
        eth_staking_yield_pct: passed to PCP / basis sources

    Returns:
        AggregatedRate with rate + per-source breakdown.
    """
    if asset != "USD":
        raise NotImplementedError(f"Day 1 supports USD only. Got: {asset!r}")

    cache_key = (asset,)
    now = int(time.time())
    if use_cache and cache_key in _RATE_CACHE:
        cached_ts, cached = _RATE_CACHE[cache_key]
        if now - cached_ts < CACHE_TTL_SEC:
            return cached

    # NOTE: aave_borrow_weth deliberately excluded — that's the WETH lending
    # market (interest paid in ETH), structurally separate from USDC short rate.
    # Kept as standalone fetch function for cross-asset references in future v2.0.
    sources = {
        "deribit_pcp_30d":      fetch_deribit_pcp_30d(eth_staking_yield_pct),
        "hl_funding_smoothed":  fetch_hl_funding_smoothed(),
        "aevo_pcp":             fetch_aevo_pcp(eth_staking_yield_pct),
        "deribit_basis_3m":     fetch_deribit_basis_3m(eth_staking_yield_pct),
        "aave_borrow_usdc":     fetch_aave_borrow_usdc(),
        "compound_borrow_usdc": fetch_compound_borrow_usdc(),
        "sofr_30d":             fetch_sofr_30d(),
    }

    # Build weighted-median input
    pairs: list[tuple[float, float]] = []
    for name, src in sources.items():
        if src.ok:
            r = src.rate_pct
            assert r is not None  # type narrow
            # Sanity clip
            if RATE_FLOOR_PCT <= r <= RATE_CEILING_PCT:
                pairs.append((r, src.weight))

    if not pairs:
        raise RuntimeError("no valid rate sources — all fetches failed")

    median_rate = weighted_median_value(pairs)
    effective_weight = sum(w for _, w in pairs)

    result = AggregatedRate(
        rate_pct=median_rate,
        sources=sources,
        effective_weight_sum=effective_weight,
        timestamp=now,
    )

    if use_cache:
        _RATE_CACHE[cache_key] = (now, result)

    return result


__all__ = [
    "RateSource",
    "AggregatedRate",
    "aggregate_rates",
    "weighted_median_value",
    "fetch_deribit_pcp_30d",
    "fetch_hl_funding_smoothed",
    "fetch_aevo_pcp",
    "fetch_deribit_basis_3m",
    "fetch_aave_borrow_usdc",
    "fetch_aave_borrow_weth",
    "fetch_compound_borrow_usdc",
    "fetch_sofr_30d",
]
