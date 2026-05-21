"""
Variance decomposition: cv (continuous) + j² (jump squared) per 5-min bar.

Mirrors the variance estimation logic of ARMSHookV3 on-chain hook —
same threshold method, same rolling-window semantics. The output
feeds directly into the Agent-SOFR rate formula:

    total_variance_per_bar = cv + λ·j²    (λ = 1.097 from calibration)

This module is pure compute over a price-return time series. Data
ingestion (fetching live OHLCV) is handled separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from oracle.calibration import (
    BAR_SECONDS, BARS_PER_HOUR, BARS_PER_DAY, LAMBDA_JUMP_WEIGHT,
)


# Threshold for declaring a return a "jump": |r| > spike_percentile of |r| distribution
DEFAULT_SPIKE_PERCENTILE: float = 95.0


@dataclass(frozen=True)
class VarianceSnapshot:
    """
    Output of variance decomposition over a rolling window of returns.

    All variance values are dimensionless (squared return units, per bar).
    σ values are also dimensionless (√variance, per bar).
    To annualize: multiply σ_per_bar by √(BARS_PER_YEAR).
    """
    timestamp: int                  # unix seconds (end of window)
    window_bars: int                # number of bars in this estimate

    # Decomposition (per-bar, not annualized)
    cv_per_bar: float               # continuous variance
    j_squared_per_bar: float        # jump variance squared
    total_per_bar: float            # cv + λ·j² — the operational total

    # Derived: σ for this window (per bar)
    sigma_5min: float               # √total_per_bar (fractional)
    sigma_5min_bp: float            # σ in basis points (1 bp = 0.0001)

    # Metadata
    spike_threshold_pct: float      # what percentile defined jumps
    n_jump_bars: int                # how many bars were declared jumps
    spike_threshold_value: float    # the actual |r| threshold used

    def annualized_sigma_pct(self) -> float:
        """σ as annualized %. Standard finance scaling: σ × √(bars/year)."""
        return self.sigma_5min * math.sqrt(288 * 365) * 100

    def variance_over_horizon(self, horizon_seconds: int) -> float:
        """
        Total variance over a future horizon T.

        Assumes returns are iid (BS world) — for non-iid, use a more
        sophisticated estimator. For sub-day horizons, iid is a reasonable
        approximation on liquid crypto pairs.
        """
        bars = horizon_seconds / BAR_SECONDS
        return self.total_per_bar * bars

    def sigma_over_horizon(self, horizon_seconds: int) -> float:
        """σ over horizon T (fractional)."""
        return math.sqrt(self.variance_over_horizon(horizon_seconds))


def compute_variance_from_returns(
    returns: Sequence[float],
    timestamp: int,
    spike_percentile: float = DEFAULT_SPIKE_PERCENTILE,
) -> VarianceSnapshot:
    """
    Decompose a return series into continuous and jump variance.

    Args:
        returns: log returns per 5-min bar, e.g. ln(P_i / P_{i-1})
        timestamp: unix seconds, end of window
        spike_percentile: returns with |r| > this percentile of |r| are
                          classified as jumps. Default 95.

    Returns:
        VarianceSnapshot with cv + j² decomposition.

    Raises:
        ValueError: if returns is empty or contains non-finite values.
    """
    arr = np.asarray(returns, dtype=float)
    if arr.size == 0:
        raise ValueError("returns array is empty")
    if not np.all(np.isfinite(arr)):
        raise ValueError("returns contains non-finite values (NaN/inf)")

    abs_r = np.abs(arr)
    threshold = float(np.percentile(abs_r, spike_percentile))

    # Classify each bar as jump or continuous
    is_jump = abs_r > threshold
    continuous_mask = ~is_jump

    # Per-bar variances: mean of r² over each subset (treats each subset
    # as a sample of per-bar variance)
    if continuous_mask.sum() > 0:
        cv_per_bar = float(np.mean(arr[continuous_mask] ** 2))
    else:
        cv_per_bar = 0.0

    if is_jump.sum() > 0:
        # j² is the squared mean magnitude of jumps; we use mean of r²
        # over jump bars (matches the formula structure)
        j_squared_per_bar = float(np.mean(arr[is_jump] ** 2))
    else:
        j_squared_per_bar = 0.0

    total_per_bar = cv_per_bar + LAMBDA_JUMP_WEIGHT * j_squared_per_bar
    sigma_5min = math.sqrt(total_per_bar) if total_per_bar > 0 else 0.0

    return VarianceSnapshot(
        timestamp=timestamp,
        window_bars=arr.size,
        cv_per_bar=cv_per_bar,
        j_squared_per_bar=j_squared_per_bar,
        total_per_bar=total_per_bar,
        sigma_5min=sigma_5min,
        sigma_5min_bp=sigma_5min * 10_000,
        spike_threshold_pct=spike_percentile,
        n_jump_bars=int(is_jump.sum()),
        spike_threshold_value=threshold,
    )


def returns_from_prices(prices: Sequence[float]) -> np.ndarray:
    """
    Compute log returns from a price series: r_i = ln(P_i / P_{i-1}).
    Output has length len(prices) - 1.
    """
    arr = np.asarray(prices, dtype=float)
    if arr.size < 2:
        return np.array([])
    return np.log(arr[1:] / arr[:-1])


class VarianceEngine:
    """
    Stateful engine maintaining a rolling window of returns + on-demand
    snapshot computation. Useful for live use where bars arrive one at a time.
    """

    def __init__(
        self,
        window_bars: int = BARS_PER_HOUR,  # 1h window (matches arms hook)
        spike_percentile: float = DEFAULT_SPIKE_PERCENTILE,
    ):
        if window_bars < 2:
            raise ValueError("window_bars must be >= 2")
        self.window_bars = window_bars
        self.spike_percentile = spike_percentile
        self._returns: list[float] = []
        self._latest_timestamp: int = 0

    def add_return(self, r: float, timestamp: int) -> None:
        """Append a new return observation. Drops oldest if window full."""
        self._returns.append(float(r))
        self._latest_timestamp = int(timestamp)
        if len(self._returns) > self.window_bars:
            self._returns.pop(0)

    def add_bar(self, price_prev: float, price_cur: float, timestamp: int) -> None:
        """Convenience: compute return from two consecutive prices."""
        if price_prev <= 0 or price_cur <= 0:
            raise ValueError(f"prices must be positive: prev={price_prev}, cur={price_cur}")
        r = math.log(price_cur / price_prev)
        self.add_return(r, timestamp)

    def snapshot(self) -> Optional[VarianceSnapshot]:
        """
        Compute variance snapshot from current window.
        Returns None if window not yet full.
        """
        if len(self._returns) < self.window_bars:
            return None
        return compute_variance_from_returns(
            self._returns,
            timestamp=self._latest_timestamp,
            spike_percentile=self.spike_percentile,
        )

    def fill_count(self) -> int:
        return len(self._returns)

    def is_ready(self) -> bool:
        return len(self._returns) >= self.window_bars

    def reset(self) -> None:
        self._returns = []
        self._latest_timestamp = 0


# ─────────────────────────────────────────────────────────────────────────────
# Live data fetcher — Binance public klines (no auth needed, no pandas needed)
# ─────────────────────────────────────────────────────────────────────────────

import json
import urllib.request


def fetch_live_eth_returns(
    n_bars: int = 24,
    symbol: str = "ETHUSDT",
    interval: str = "5m",
) -> tuple[np.ndarray, list[int]]:
    """
    Fetch the last N 5-min closes from Binance public klines API.

    Returns (returns, timestamps) — same as the parquet loader, suitable
    for direct feed into compute_variance_from_returns.

    Args:
        n_bars: number of return observations needed (will fetch n_bars+1 candles)
        symbol: trading pair (default ETHUSDT)
        interval: kline interval (default 5m)

    Returns:
        (returns_array, timestamps_unix)
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval={interval}&limit={n_bars + 1}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "agent-sofr/0.1"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    # Binance kline format: [open_time_ms, open, high, low, close, ...]
    closes = np.array([float(row[4]) for row in data])
    open_times_ms = [int(row[0]) for row in data]

    if len(closes) < 2:
        raise RuntimeError(f"fetched only {len(closes)} candles, need >=2")

    returns = returns_from_prices(closes)
    # Timestamp for return[i] = open_time of bar[i+1] (closing observation epoch)
    timestamps = [open_times_ms[i + 1] // 1000 for i in range(len(returns))]
    return returns, timestamps


# ─────────────────────────────────────────────────────────────────────────────
# Reference data loader — for replay tests using arms/research/ethusdt_5m.parquet
# Local dev only. Production uses fetch_live_eth_returns().
# ─────────────────────────────────────────────────────────────────────────────

ARMS_REFERENCE_PARQUET = Path("/Users/dz/arms/research/ethusdt_5m.parquet")


def load_reference_ethusdt_returns(
    n_recent_bars: Optional[int] = None,
) -> tuple[np.ndarray, list[int]]:
    """
    Load ETH/USDT 5-min returns from arms research dataset.

    Raises FileNotFoundError if parquet missing — fall back to fetch_live_eth_returns().
    """
    if not ARMS_REFERENCE_PARQUET.exists():
        raise FileNotFoundError(
            f"Reference dataset not found at {ARMS_REFERENCE_PARQUET}. "
            "Use fetch_live_eth_returns() in production."
        )

    import pandas as pd  # type: ignore[import-untyped]
    df = pd.read_parquet(ARMS_REFERENCE_PARQUET)
    if n_recent_bars is not None:
        df = df.tail(n_recent_bars + 1)

    prices = df["close"].values
    returns = returns_from_prices(prices)
    timestamps = (df.index[1:].astype(np.int64) // 1_000_000_000).tolist()
    return returns, timestamps


__all__ = [
    "VarianceSnapshot",
    "VarianceEngine",
    "compute_variance_from_returns",
    "returns_from_prices",
    "fetch_live_eth_returns",
    "load_reference_ethusdt_returns",
    "DEFAULT_SPIKE_PERCENTILE",
]
