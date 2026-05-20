"""
Pure VRP computation, decoupled from agent runtime.

Pulls Deribit ETH-PERPETUAL hourly OHLC + DVOL, computes
Parkinson realized vol over 72h, returns VRP = DVOL − R_long.

Cached for 60s to avoid hammering Deribit.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


DERIBIT_BASE = "https://www.deribit.com/api/v2"
HISTORY_HOURS = 240          # 10d window — enough to warm 72h rolling
WIN_LONG_H    = 72
WIN_SHORT_H   = 6
CACHE_TTL_SEC = 60


_cache = {"ts": 0, "data": None}


def _get(url: str, retries: int = 3, timeout: int = 15):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def fetch_ohlc(symbol: str = "ETH", hours: int = HISTORY_HOURS) -> pd.DataFrame:
    end_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    url = (f"{DERIBIT_BASE}/public/get_tradingview_chart_data"
           f"?instrument_name={symbol}-PERPETUAL"
           f"&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution=60")
    j = _get(url)["result"]
    df = pd.DataFrame({
        "ts":    j["ticks"],
        "open":  j["open"],
        "high":  j["high"],
        "low":   j["low"],
        "close": j["close"],
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop(columns=["ts"]).sort_values("datetime").reset_index(drop=True)


def fetch_dvol(currency: str = "ETH", hours: int = HISTORY_HOURS) -> pd.DataFrame:
    end_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    url = (f"{DERIBIT_BASE}/public/get_volatility_index_data"
           f"?currency={currency}"
           f"&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution=3600")
    j = _get(url)["result"]
    arr = np.array(j["data"])
    df = pd.DataFrame({"ts": arr[:, 0], "dvol": arr[:, 4]})  # close col
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["datetime", "dvol"]].sort_values("datetime").reset_index(drop=True)


def parkinson(high: pd.Series, low: pd.Series, win: int) -> pd.Series:
    """Annualized Parkinson realized vol (%) on rolling `win` hourly bars."""
    c   = 1.0 / (4.0 * np.log(2.0))
    ann = 365.0 * 24.0
    x = np.log(high / low).replace([np.inf, -np.inf], np.nan).pow(2)
    return np.sqrt(c * x.rolling(win, min_periods=win).mean()) * np.sqrt(ann) * 100.0


@dataclass
class VRPSnapshot:
    timestamp_utc: str        # ISO 8601
    asset: str                # "ETH"
    spot:        float        # USD
    dvol:        float        # %, Deribit volatility index
    rv_72h:      float        # %, Parkinson realized vol over 72h
    rv_6h:       float        # %, Parkinson realized vol over 6h
    vrp:         float        # %, dvol − rv_72h
    regime: str               # LOW (rv_72h<40), MID (40-60), HIGH (>60)
    quiet:       bool         # rv_72h < 60
    window:      dict         # methodology params

    def to_dict(self) -> dict:
        return asdict(self)


def compute_vrp(asset: str = "ETH", force_refresh: bool = False) -> VRPSnapshot:
    now = time.time()
    if (not force_refresh
            and _cache["data"] is not None
            and _cache["data"].asset == asset
            and now - _cache["ts"] < CACHE_TTL_SEC):
        return _cache["data"]

    ohlc = fetch_ohlc(asset, HISTORY_HOURS)
    dvol = fetch_dvol(asset, HISTORY_HOURS)
    df = pd.merge_asof(
        ohlc.sort_values("datetime"),
        dvol.sort_values("datetime"),
        on="datetime", direction="nearest",
    )
    df["rv_72h"] = parkinson(df["high"], df["low"], WIN_LONG_H)
    df["rv_6h"]  = parkinson(df["high"], df["low"], WIN_SHORT_H)
    last = df.iloc[-1]

    rv = float(last["rv_72h"])
    regime = "LOW" if rv < 40 else ("MID" if rv < 60 else "HIGH")

    snap = VRPSnapshot(
        timestamp_utc=last["datetime"].isoformat(),
        asset=asset,
        spot=float(last["close"]),
        dvol=float(last["dvol"]),
        rv_72h=rv,
        rv_6h=float(last["rv_6h"]),
        vrp=float(last["dvol"] - last["rv_72h"]),
        regime=regime,
        quiet=rv < 60,
        window={"rv_72h_hours": WIN_LONG_H, "rv_6h_hours": WIN_SHORT_H,
                "annualizer": "Parkinson_log_HL_ratio_sqrt_365x24"},
    )
    _cache["ts"] = now
    _cache["data"] = snap
    return snap


if __name__ == "__main__":
    snap = compute_vrp("ETH")
    print(json.dumps(snap.to_dict(), indent=2))
