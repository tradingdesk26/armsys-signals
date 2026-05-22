"""
Agent-SOFR — top-level oracle composing all sub-modules.

Produces the published rate quote with full decomposition:

    agent_sofr(asset, horizon) =
        base_anchor(asset)                  # weighted median from 8 sources
      + variance_premium(asset, horizon)    # (cv + λ·j²) × T → LTV × P_default
      + regime_adjustment(asset)            # 6-mode ladder, calibrated

Public API matches the methodology doc — same fields names, same units.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from scipy.stats import norm

from oracle.calibration import (
    METHODOLOGY_VERSION, METHODOLOGY_IPFS_HASH, METHODOLOGY_CONTENT_HASH_SHA256,
    CALIBRATION_SOURCE, CALIBRATION_DATA,
    REGIME_PREMIUM_BPS, REGIME_MAX_LTV, MATCHING_PAUSE_REGIMES,
    LGD_DEFAULT, DEFAULT_MAX_DEFAULT_PROB,
    SIGMA_CUTS, LAMBDA_JUMP_WEIGHT, HYSTERESIS_EPS_DOWN,
    CACHE_TTL_SEC, RATE_FLOOR_PCT, RATE_CEILING_PCT,
    BARS_PER_YEAR,
)
from oracle.rate_aggregator import aggregate_rates, AggregatedRate
from oracle.variance_engine import (
    VarianceSnapshot, compute_variance_from_returns,
    load_reference_ethusdt_returns, fetch_live_eth_returns,
)
from oracle.regime_classifier import RegimeClassifier, ClassificationResult
from oracle.max_ltv import max_safe_ltv, MaxLTVResult


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot dataclass — JSON-friendly via to_dict()
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentSOFRSnapshot:
    """Full Agent-SOFR rate quote with decomposition + provenance."""

    # Identification
    asset: str                     # "USD" | "EUR" | "ETH"
    horizon_sec: int               # loan horizon in seconds
    horizon_label: str             # human label ("1h", "4h", etc.)

    # The published rate (annualized %)
    rate_pct: float

    # Decomposition
    base_anchor_pct: float
    variance_premium_pct: float
    regime_adjustment_pct: float

    # Variance state
    cv_per_bar: float
    j_squared_per_bar: float
    lambda_jump_weight: float
    sigma_5min_bp: float
    sigma_horizon_pct: float       # σ_T as %, scaled to horizon

    # Regime
    regime_name: str
    regime_index: int
    sigma_5min_thresholds_bp: dict[str, float]   # named thresholds for transparency

    # Aggregator detail
    aggregator_sources: dict        # name → {rate_pct, weight, ok, error?}
    effective_weight_sum: float

    # Time
    computed_at: int
    valid_until: int
    cache_ttl_sec: int

    # Provenance
    methodology_version: str
    methodology_url: str
    methodology_ipfs_hash: str
    methodology_content_hash_sha256: str
    calibration_source: str
    calibration_data: str

    def to_dict(self) -> dict:
        """JSON-friendly representation for API response."""
        return {
            "ok": True,
            "asset": self.asset,
            "horizon": self.horizon_label,
            "horizon_sec": self.horizon_sec,
            "rate": round(self.rate_pct, 4),
            "decomposition": {
                "base_anchor": round(self.base_anchor_pct, 4),
                "variance_premium": round(self.variance_premium_pct, 4),
                "regime_adjustment": round(self.regime_adjustment_pct, 4),
            },
            "variance": {
                "cv_per_bar": self.cv_per_bar,
                "j_squared_per_bar": self.j_squared_per_bar,
                "lambda_jump_weight": self.lambda_jump_weight,
                "sigma_5min_bp": round(self.sigma_5min_bp, 3),
                "sigma_horizon_pct": round(self.sigma_horizon_pct, 4),
            },
            "regime": {
                "mode": self.regime_name,
                "mode_index": self.regime_index,
                "thresholds_bp": self.sigma_5min_thresholds_bp,
            },
            "sources": {
                name: {
                    "rate_pct": round(d["rate_pct"], 4) if d.get("rate_pct") is not None else None,
                    "weight": d["weight"],
                    "ok": d["ok"],
                    "error": d.get("error"),
                }
                for name, d in self.aggregator_sources.items()
            },
            "effective_weight_sum": round(self.effective_weight_sum, 3),
            "methodology": {
                "version": self.methodology_version,
                "url": self.methodology_url,
                "content_hash_sha256": self.methodology_content_hash_sha256,
                "ipfs_cid": self.methodology_ipfs_hash,
                "ipfs_gateways": [
                    f"https://ipfs.io/ipfs/{self.methodology_ipfs_hash}",
                    f"https://dweb.link/ipfs/{self.methodology_ipfs_hash}",
                    f"https://w3s.link/ipfs/{self.methodology_ipfs_hash}",
                ] if self.methodology_ipfs_hash else [],
                "calibration_source": self.calibration_source,
                "calibration_data": self.calibration_data,
                "verify": {
                    "https": (
                        f"curl {self.methodology_url} | shasum -a 256  "
                        f"# should equal content_hash_sha256"
                    ),
                    "ipfs": (
                        f"curl -H 'Accept: application/vnd.ipld.raw' "
                        f"https://ipfs.io/ipfs/{self.methodology_ipfs_hash} | shasum -a 256"
                    ) if self.methodology_ipfs_hash else None,
                },
            },
            "computed_at": self.computed_at,
            "valid_until": self.valid_until,
            "cache_ttl_sec": self.cache_ttl_sec,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Module-level state — variance engine + regime classifier are stateful
# ─────────────────────────────────────────────────────────────────────────────

# Single global classifier (stateful for hysteresis)
_REGIME_CLASSIFIER: Optional[RegimeClassifier] = None


def get_regime_classifier() -> RegimeClassifier:
    global _REGIME_CLASSIFIER
    if _REGIME_CLASSIFIER is None:
        _REGIME_CLASSIFIER = RegimeClassifier()
    return _REGIME_CLASSIFIER


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_HORIZON_LABELS = {
    60:    "1m",
    300:   "5m",
    1800:  "30m",
    3600:  "1h",
    14400: "4h",
    86400: "24h",
}


def _horizon_label(sec: int) -> str:
    return _HORIZON_LABELS.get(sec, f"{sec}s")


def _compute_variance_premium_pct(
    variance: VarianceSnapshot,
    duration_sec: int,
    ltv: float,
    max_default_prob: float = DEFAULT_MAX_DEFAULT_PROB,
    lgd: float = LGD_DEFAULT,
) -> float:
    """
    Variance premium component (annualized %).

    For a loan of duration T:
        σ_T = √(total_variance_per_bar × bars(T))
        P_default(LTV, σ_T) = Φ(-ln(1/LTV) / σ_T)   (Black-Cox first-passage)
        annual_premium = LTV × P_default × LGD × (bars_per_year / bars(T))

    The (bars_per_year / bars(T)) factor scales the per-horizon expected loss
    to an annualized rate.
    """
    bars = duration_sec / 300
    if bars <= 0:
        return 0.0

    sigma_T = variance.sigma_over_horizon(duration_sec)
    if sigma_T <= 0 or ltv <= 0 or ltv >= 1:
        return 0.0

    z = -math.log(1.0 / ltv) / sigma_T  # negative when LTV < 1
    # Default prob = Φ(z) since z is already negative (we want left tail)
    # But conventionally written: P_default = Φ(-distance/σ_T)
    p_default = norm.cdf(z)

    expected_loss_fraction = ltv * p_default * lgd
    # Annualize: scale per-horizon loss to annual rate
    annual_loss_pct = expected_loss_fraction * (BARS_PER_YEAR / bars) * 100
    return annual_loss_pct


# ─────────────────────────────────────────────────────────────────────────────
# Module-level snapshot cache (one per (asset, horizon, ltv))
# ─────────────────────────────────────────────────────────────────────────────

_SNAPSHOT_CACHE: dict[tuple, tuple[int, AgentSOFRSnapshot]] = {}


def compute_agent_sofr(
    asset: str,
    horizon_sec: int,
    ltv_for_premium: float = 0.80,   # representative LTV for variance premium calc
    use_cache: bool = True,
) -> AgentSOFRSnapshot:
    """
    Compute the published Agent-SOFR rate.

    Day 1 implementation: variance from ARMS reference dataset (last 24 bars
    = 2 hours of ETH/USDT 5-min data). Live price feed integration is Day 2.

    Args:
        asset: "USD" | "EUR" | "ETH" — only "USD" supported in Day 1
        horizon_sec: loan duration for variance/jump premium calc
        ltv_for_premium: representative LTV for the premium calc (default 80%)
        use_cache: return cached result if fresh

    Returns:
        AgentSOFRSnapshot with full decomposition + provenance.
    """
    if asset != "USD":
        raise NotImplementedError(f"Day 1 supports USD only. Got: {asset!r}")
    if horizon_sec <= 0:
        raise ValueError(f"horizon_sec must be positive, got {horizon_sec}")

    cache_key = (asset, horizon_sec, round(ltv_for_premium, 3))
    now = int(time.time())
    if use_cache and cache_key in _SNAPSHOT_CACHE:
        cached_ts, cached = _SNAPSHOT_CACHE[cache_key]
        if now - cached_ts < CACHE_TTL_SEC:
            return cached

    # Step 1 — Variance from ETH 5-min bars (live Binance) with parquet fallback
    try:
        returns, timestamps = fetch_live_eth_returns(n_bars=24)
    except Exception:
        # Fall back to local reference if live fetch fails
        returns, timestamps = load_reference_ethusdt_returns(n_recent_bars=24)
    variance = compute_variance_from_returns(returns, timestamp=timestamps[-1])

    # Step 2 — Regime classification (uses σ_5min)
    clf = get_regime_classifier()
    regime_result = clf.classify(variance.sigma_5min)

    # Step 3 — Multi-source base anchor
    aggregated = aggregate_rates(asset="USD", use_cache=use_cache)

    # Step 4 — Variance premium for representative LTV
    variance_premium = _compute_variance_premium_pct(
        variance=variance,
        duration_sec=horizon_sec,
        ltv=ltv_for_premium,
    )

    # Step 5 — Regime adjustment (bps → %)
    regime_adj_pct = REGIME_PREMIUM_BPS[regime_result.mode_name] / 100.0

    # Step 6 — Compose
    rate_pct = aggregated.rate_pct + variance_premium + regime_adj_pct

    # Sanity clip
    if not (RATE_FLOOR_PCT <= rate_pct <= RATE_CEILING_PCT):
        # Don't publish — caller should fall back to last-known-good
        raise RuntimeError(
            f"Agent-SOFR sanity-clip violation: rate={rate_pct:.3f}% outside "
            f"[{RATE_FLOOR_PCT}, {RATE_CEILING_PCT}]"
        )

    # Build response
    thresholds_bp = {
        "p50": SIGMA_CUTS[0] * 10000,
        "p65": SIGMA_CUTS[1] * 10000,
        "p80": SIGMA_CUTS[2] * 10000,
        "p93": SIGMA_CUTS[3] * 10000,
        "p99": SIGMA_CUTS[4] * 10000,
    }

    aggregator_sources_dict = {
        name: {
            "rate_pct": src.rate_pct,
            "weight": src.weight,
            "ok": src.ok,
            "error": src.error,
        }
        for name, src in aggregated.sources.items()
    }

    snapshot = AgentSOFRSnapshot(
        asset=asset,
        horizon_sec=horizon_sec,
        horizon_label=_horizon_label(horizon_sec),
        rate_pct=rate_pct,
        base_anchor_pct=aggregated.rate_pct,
        variance_premium_pct=variance_premium,
        regime_adjustment_pct=regime_adj_pct,
        cv_per_bar=variance.cv_per_bar,
        j_squared_per_bar=variance.j_squared_per_bar,
        lambda_jump_weight=LAMBDA_JUMP_WEIGHT,
        sigma_5min_bp=variance.sigma_5min_bp,
        sigma_horizon_pct=variance.sigma_over_horizon(horizon_sec) * 100,
        regime_name=regime_result.mode_name,
        regime_index=regime_result.mode_index,
        sigma_5min_thresholds_bp=thresholds_bp,
        aggregator_sources=aggregator_sources_dict,
        effective_weight_sum=aggregated.effective_weight_sum,
        computed_at=now,
        valid_until=now + CACHE_TTL_SEC,
        cache_ttl_sec=CACHE_TTL_SEC,
        methodology_version=METHODOLOGY_VERSION,
        methodology_url=f"https://regimeshift.xyz/methodology/{METHODOLOGY_VERSION}",
        methodology_ipfs_hash=METHODOLOGY_IPFS_HASH,
        methodology_content_hash_sha256=METHODOLOGY_CONTENT_HASH_SHA256,
        calibration_source=CALIBRATION_SOURCE,
        calibration_data=CALIBRATION_DATA,
    )

    if use_cache:
        _SNAPSHOT_CACHE[cache_key] = (now, snapshot)

    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Max-LTV endpoint (also exposed for /v1/risk/max-ltv)
# ─────────────────────────────────────────────────────────────────────────────

def compute_max_ltv_for_loan(
    asset: str,
    duration_sec: int,
    max_default_prob: float = DEFAULT_MAX_DEFAULT_PROB,
) -> MaxLTVResult:
    """
    Compute max safe LTV for given loan. Uses same variance + regime state
    as the SOFR rate snapshot.

    Returns MaxLTVResult ready to JSON-serialize via .to_dict().
    """
    if asset != "ETH" and asset != "USD":
        # Day 1: ETH-collateralized loans only (USD is the principal anchor)
        raise NotImplementedError(f"Day 1 supports ETH collateral only. Got: {asset!r}")

    try:
        returns, timestamps = fetch_live_eth_returns(n_bars=24)
    except Exception:
        returns, timestamps = load_reference_ethusdt_returns(n_recent_bars=24)
    variance = compute_variance_from_returns(returns, timestamp=timestamps[-1])

    clf = get_regime_classifier()
    regime_result = clf.classify(variance.sigma_5min)

    return max_safe_ltv(
        variance=variance,
        duration_sec=duration_sec,
        regime=regime_result.mode_name,
        max_default_prob=max_default_prob,
        asset="ETH",
    )


__all__ = [
    "AgentSOFRSnapshot",
    "compute_agent_sofr",
    "compute_max_ltv_for_loan",
    "get_regime_classifier",
]
