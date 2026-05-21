"""
ARMS Signals API — derived market metrics for AI agents.

Endpoints (versioned):
  GET  /                          — service status (free)
  GET  /health                    — liveness (free)
  GET  /v1/asset/{asset}/vrp      — VRP for ETH or BTC (PAID via x402)

Paid endpoints accept x402 USDC nanopayments on Base mainnet.
Free clients receive HTTP 402 with payment requirements; pay then
re-request and receive the data.

Response includes the metric value, raw inputs (audit trail), timestamp,
and methodology URL.
"""
from __future__ import annotations

import os
import time
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network
from x402.server import x402ResourceServer

# Bazaar discovery extension — required for CDP Bazaar to catalog the
# endpoint. Without it, the validator at agentic.market/validate rejects
# the listing because the 402 envelope is missing extensions.bazaar.
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    declare_discovery_extension,
)
from x402.extensions.bazaar.resource_service import OutputConfig

from dotenv import load_dotenv
from starlette.middleware.base import BaseHTTPMiddleware

import signals
from stats import StatsCounter

# Agent-SOFR oracle (multi-source rate + max-LTV + variance engine)
from oracle.agent_sofr import compute_agent_sofr, compute_max_ltv_for_loan

load_dotenv("/opt/arms-signals/.env")  # absolute path so it loads under systemd

stats = StatsCounter()

METHODOLOGY_BASE = "https://regimeshift.xyz/methodology"

# x402 config. Mode is decided by presence of CDP_API_KEY_ID:
#   - With CDP creds → Coinbase CDP facilitator on Base MAINNET (real USDC).
#   - Without        → default x402.org facilitator on Base Sepolia (testnet).
EVM_ADDRESS = os.getenv("EVM_ADDRESS", "0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B")
CDP_API_KEY_ID     = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")

if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    # CDP /supported lists both "base" (v1) and "eip155:8453" (v2). Use
    # the v2 CAIP-2 form since the x402 SDK in this repo is on v2.
    EVM_NETWORK: Network = os.getenv("EVM_NETWORK", "eip155:8453")
    FACILITATOR_URL = os.getenv(
        "FACILITATOR_URL",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    )
    USE_CDP = True
else:
    EVM_NETWORK = os.getenv("EVM_NETWORK", "eip155:84532")  # Base Sepolia v2
    FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
    USE_CDP = False


def _cdp_create_headers() -> dict[str, dict[str, str]]:
    """Generate fresh CDP JWTs for each facilitator endpoint.

    The CDP facilitator at api.cdp.coinbase.com/platform/v2/x402 expects
    Bearer JWTs signed with the API key's Ed25519 secret, bound to the
    HTTP method + host + path of the actual upstream call.
    """
    from cdp.auth.utils.http import generate_jwt
    from cdp.auth.utils.jwt import JwtOptions
    host = "api.cdp.coinbase.com"
    base = "/platform/v2/x402"  # CDP facilitator base path

    def _hdr(method: str, path: str) -> dict[str, str]:
        jwt = generate_jwt(JwtOptions(
            api_key_id=CDP_API_KEY_ID,
            api_key_secret=CDP_API_KEY_SECRET,
            request_method=method,
            request_host=host,
            request_path=path,
        ))
        return {"Authorization": f"Bearer {jwt}"}

    return {
        "verify":    _hdr("POST", f"{base}/verify"),
        "settle":    _hdr("POST", f"{base}/settle"),
        "supported": _hdr("GET",  f"{base}/supported"),
        "list":      _hdr("GET",  f"{base}/discovery/resources"),
    }

app = FastAPI(
    title="ARMS Signals API",
    description="Derived market metrics for AI agents — vol regime, VRP, "
                 "skew anomalies, intraweek bias. Reference customer: the "
                 "RegimeShift autonomous agent on regimeshift.xyz.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


class _StatsMiddleware(BaseHTTPMiddleware):
    """Increment per-(path, status) counters on every response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path or "/"
        # Group by status family: 402 = preview, 200 = paid call, * = other
        if response.status_code == 402:
            bucket = "402"
        elif 200 <= response.status_code < 300:
            bucket = "200"
        else:
            bucket = "other"
        stats.incr(f"{path}.{bucket}")
        stats.incr(f"{path}.total")
        stats.incr(f"_total.{bucket}")
        stats.incr("_total.requests")
        return response


# NOTE: Stats middleware needs to be the OUTERMOST so it sees the final
# response status — including 402s that x402's PaymentMiddlewareASGI
# returns short-circuit. Starlette processes middleware last-added first,
# so we register x402 paywall first, then stats on top.
# (Look further down in this file for `app.add_middleware(_StatsMiddleware)`
# placed AFTER the x402 setup.)

# ─── x402 paywall ─────────────────────────────────────────────────────
_fac_config_kwargs = {"url": FACILITATOR_URL}
if USE_CDP:
    _fac_config_kwargs["auth_provider"] = CreateHeadersAuthProvider(
        _cdp_create_headers
    )

facilitator = HTTPFacilitatorClient(FacilitatorConfig(**_fac_config_kwargs))
x402_server = x402ResourceServer(facilitator)
x402_server.register(EVM_NETWORK, ExactEvmServerScheme())

# Bazaar discovery — enriches each 402 with `extensions.bazaar.info` so the
# CDP Bazaar indexer can catalog the endpoint. Without this the validator
# at agentic.market/validate rejects the listing.
x402_server.register_extension(bazaar_resource_server_extension)


# ─── Sample output + JSON Schema used by Bazaar's discovery probe ────
_VRP_SAMPLE_OUTPUT = {
    "ok": True,
    "asset": "ETH",
    "vrp": 0.1525,
    "regime": "MID",
    "quiet": True,
    "inputs": {
        "dvol":      52.58,
        "rv_72h":    52.4275,
        "rv_6h":     40.204,
        "spot_usd":  2130.2,
        "timestamp": "2026-05-20T14:00:00+00:00",
        "source":    "Deribit public API (DVOL + ETH-PERPETUAL OHLC)",
    },
    "methodology":  "https://regimeshift.xyz/methodology/vrp-v1",
    "computed_at":  1779284275,
    "cache_ttl_sec": 60,
}

_VRP_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok":     {"type": "boolean", "description": "Request status"},
        "asset":  {"type": "string",  "enum": ["ETH", "BTC"]},
        "vrp":    {"type": "number",  "description":
                    "DVOL − Parkinson RV(72h), in vol points. "
                    "Positive = sell-vol opportunity"},
        "regime": {"type": "string",  "enum": ["LOW", "MID", "HIGH"]},
        "quiet":  {"type": "boolean", "description": "true if RV_72h < 60%"},
        "inputs": {
            "type": "object",
            "properties": {
                "dvol":      {"type": "number", "description":
                               "Deribit volatility index, annualized %"},
                "rv_72h":    {"type": "number", "description":
                               "Parkinson realized vol over 72h, %"},
                "rv_6h":     {"type": "number", "description":
                               "Parkinson realized vol over 6h, %"},
                "spot_usd":  {"type": "number"},
                "timestamp": {"type": "string", "format": "date-time"},
                "source":    {"type": "string"},
            },
            "required": ["dvol", "rv_72h", "spot_usd", "timestamp"],
        },
        "methodology":   {"type": "string", "format": "uri"},
        "computed_at":   {"type": "integer", "description": "Unix timestamp"},
        "cache_ttl_sec": {"type": "integer"},
    },
    "required": ["ok", "asset", "vrp", "regime", "inputs"],
}

_VRP_DISCOVERY = declare_discovery_extension(
    input={},  # GET with no query params; empty example keeps probe happy
    output=OutputConfig(
        example=_VRP_SAMPLE_OUTPUT,
        schema=_VRP_OUTPUT_SCHEMA,
    ),
)

# Pricing config — keyed by "METHOD /path", wildcards supported.
def _vrp_route(asset: str) -> RouteConfig:
    """Build a per-asset RouteConfig with bazaar discovery extension."""
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price="$0.001",
                network=EVM_NETWORK,
            ),
        ],
        mime_type="application/json",
        description=(
            f"{asset} Volatility Risk Premium — DVOL minus Parkinson "
            f"realized vol (72h). Positive = sell-vol opportunity, "
            f"negative = buy-vol. Response includes regime classification "
            f"(LOW/MID/HIGH), raw inputs for audit (DVOL, RV_72h, RV_6h, "
            f"spot), timestamp, and open methodology URL."
        ),
        extensions=_VRP_DISCOVERY,
    )


# ─── Agent-SOFR Bazaar discovery samples ──────────────────────────────
_SOFR_SAMPLE_OUTPUT = {
    "ok": True,
    "asset": "USD",
    "horizon": "1h",
    "horizon_sec": 3600,
    "rate": 4.115,
    "decomposition": {
        "base_anchor": 4.115,
        "variance_premium": 0.0,
        "regime_adjustment": 0.0,
    },
    "regime": {"mode": "RESTING", "mode_index": 0},
    "methodology": {
        "version": "agent-sofr-v1",
        "url": "https://regimeshift.xyz/methodology/agent-sofr-v1",
    },
    "computed_at": 1779380000,
    "cache_ttl_sec": 60,
}

_SOFR_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok":     {"type": "boolean"},
        "asset":  {"type": "string"},
        "horizon": {"type": "string"},
        "rate":   {"type": "number", "description":
                    "Annualized agent-native short rate, %"},
        "decomposition": {"type": "object"},
        "regime": {"type": "object"},
        "sources": {"type": "object"},
        "methodology": {"type": "object"},
        "computed_at": {"type": "integer"},
    },
    "required": ["ok", "asset", "rate", "decomposition"],
}

_SOFR_DISCOVERY = declare_discovery_extension(
    input={},
    output=OutputConfig(example=_SOFR_SAMPLE_OUTPUT, schema=_SOFR_OUTPUT_SCHEMA),
)


def _sofr_route(asset: str) -> RouteConfig:
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price="$0.001",
                network=EVM_NETWORK,
            ),
        ],
        mime_type="application/json",
        description=(
            f"Agent-SOFR — decentralized {asset} short-rate benchmark for "
            f"AI agents. Aggregates 8 market-derived + reference sources "
            f"(Deribit PCP, HL funding, Aevo, Deribit basis, Aave V3, Compound, "
            f"SOFR), takes weighted median, adds variance + regime premiums. "
            f"6-mode regime classifier inherited from ARMSHookV3 production hook "
            f"(730d ETH/USDT calibration, 210k bars). Methodology open + IPFS-pinned."
        ),
        extensions=_SOFR_DISCOVERY,
    )


# ─── Max-LTV Bazaar discovery sample ─────────────────────────────────
_MAX_LTV_SAMPLE = {
    "ok": True,
    "max_ltv": 0.92,
    "math_max_ltv": 0.97,
    "regime_cap_ltv": 0.92,
    "binding_constraint": "regime_cap",
    "regime": "NORMAL",
    "sigma_T": 0.0051,
    "computed_at": 1779380000,
}

_MAX_LTV_SCHEMA = {
    "type": "object",
    "properties": {
        "ok":      {"type": "boolean"},
        "max_ltv": {"type": "number"},
        "math_max_ltv": {"type": "number"},
        "regime_cap_ltv": {"type": "number"},
        "binding_constraint": {"type": "string"},
        "regime": {"type": "string"},
        "sigma_T": {"type": "number"},
    },
    "required": ["ok", "max_ltv"],
}

_MAX_LTV_DISCOVERY = declare_discovery_extension(
    input={},
    output=OutputConfig(example=_MAX_LTV_SAMPLE, schema=_MAX_LTV_SCHEMA),
)


def _max_ltv_route() -> RouteConfig:
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price="$0.001",
                network=EVM_NETWORK,
            ),
        ],
        mime_type="application/json",
        description=(
            "Maximum-safe LTV for collateralized agent loans. Computed from "
            "(cv + λ·j²)-derived variance over loan horizon + 6-mode regime cap. "
            "Lender-specifiable max_default_prob. Returns binding constraint "
            "(math vs regime_cap) so agents can see WHY the cap is what it is. "
            "Same calibrator as Agent-SOFR rate endpoint."
        ),
        extensions=_MAX_LTV_DISCOVERY,
    )


# Explicit per-asset routes so each gets its own Bazaar listing and the
# resource URL maps cleanly to a static routeTemplate (no wildcards that
# might confuse the validator's :var1 normalisation).
x402_routes = {
    "GET /api/v1/asset/eth/vrp":   _vrp_route("ETH"),
    "GET /api/v1/asset/btc/vrp":   _vrp_route("BTC"),
    "GET /api/v1/rate/sofr/usd":   _sofr_route("USD"),
    "GET /api/v1/risk/max-ltv":    _max_ltv_route(),
}
app.add_middleware(PaymentMiddlewareASGI,
                    routes=x402_routes, server=x402_server)

# Stats wrapper sits OUTSIDE x402 so it sees the final status code
# (402 short-circuits get counted as probes; 200 paid calls land here too).
app.add_middleware(_StatsMiddleware)


@app.get("/")
def root():
    return {
        "service":   "ARMS Signals API",
        "version":   "0.1.0",
        "status":    "ok",
        "endpoints": [
            "/v1/asset/eth/vrp",
            "/v1/asset/btc/vrp",
            "/v1/rate/sofr/usd",
            "/v1/risk/max-ltv",
        ],
        "dashboard": "https://regimeshift.xyz",
        "github":    "https://github.com/tradingdesk26",
        "x402": {
            "status":     "live",
            "network":    EVM_NETWORK,
            "pay_to":     EVM_ADDRESS,
            "price":      "$0.001 per call",
            "facilitator": FACILITATOR_URL,
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "uptime_sec": int(time.time() - _STARTED)}


@app.get("/stats")
def stats_endpoint():
    """Aggregate request counts (free, for the public dashboard).

    Buckets per endpoint:
      - .402   unpaid probe (also includes wallet-less curl checks)
      - .200   successful paid call (CDP verify+settle landed)
      - .other 4xx/5xx for that path
      - .total all responses
    """
    snap = stats.snapshot()
    return {
        "started_unix":     snap["started"],
        "uptime_sec":       snap["uptime_sec"],
        "counts":           snap["counts"],
        "endpoint_summary": _endpoint_summary(snap["counts"]),
    }


def _endpoint_summary(counts: dict[str, int]) -> dict:
    """Roll up per-endpoint counts into a flat human-friendly summary."""
    endpoints = ["/api/v1/asset/eth/vrp", "/api/v1/asset/btc/vrp",
                  "/api/v1/rate/sofr/usd", "/api/v1/risk/max-ltv",
                  "/v1/asset/eth/vrp", "/v1/asset/btc/vrp",
                  "/v1/rate/sofr/usd", "/v1/risk/max-ltv"]
    out = {}
    seen = set()
    for ep in endpoints:
        if ep in seen: continue
        seen.add(ep)
        paid   = counts.get(f"{ep}.200",   0)
        probes = counts.get(f"{ep}.402",   0)
        other  = counts.get(f"{ep}.other", 0)
        total  = counts.get(f"{ep}.total", 0)
        if total > 0:
            out[ep] = {"paid": paid, "probes": probes,
                       "other": other, "total": total}
    return out


@app.get("/v1/rate/sofr/usd")
def get_agent_sofr_usd(horizon: str = "1h"):
    """
    Agent-SOFR — decentralized USD short-rate benchmark for AI agents.

    Aggregates 8 sources via weighted median, adds variance + regime premiums.
    Same calibrator (σ thresholds + λ=1.097) as our production Uniswap v4 hook.
    """
    horizon_map = {"1m": 60, "5m": 300, "30m": 1800, "1h": 3600,
                    "4h": 14400, "24h": 86400}
    horizon_sec = horizon_map.get(horizon, 3600)
    try:
        snap = compute_agent_sofr(asset="USD", horizon_sec=horizon_sec, use_cache=True)
    except Exception as e:
        raise HTTPException(status_code=503,
                              detail=f"Agent-SOFR composition failed: {e}")
    return JSONResponse(snap.to_dict())


@app.get("/v1/risk/max-ltv")
def get_max_ltv(
    asset: str = "ETH",
    duration_sec: int = 3600,
    max_default_prob: float = 0.001,
):
    """
    Max-safe LTV for collateralized agent loan.

    Returns the smaller of:
      (1) Math max from variance + lender's max_default_prob
      (2) Regime hard cap (additional jump-risk protection)

    binding_constraint field tells which one is active.
    """
    try:
        result = compute_max_ltv_for_loan(
            asset=asset.upper(),
            duration_sec=duration_sec,
            max_default_prob=max_default_prob,
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"max-LTV computation failed: {e}")
    payload = result.to_dict()
    payload["ok"] = True
    payload["methodology"] = {
        "version": "agent-sofr-v1",
        "url": "https://regimeshift.xyz/methodology/agent-sofr-v1",
    }
    payload["computed_at"] = int(time.time())
    return JSONResponse(payload)


@app.get("/v1/asset/{asset}/vrp")
def get_vrp(asset: Literal["eth", "btc", "ETH", "BTC"]):
    """Volatility Risk Premium = DVOL (annualised) − Parkinson RV(72h).

    Positive VRP = vol overpriced (sell vol opportunity).
    Negative VRP = vol underpriced (buy vol opportunity).

    Inputs included for audit. Methodology fully open at
    /methodology/vrp-v1.
    """
    asset_upper = asset.upper()
    if asset_upper not in ("ETH", "BTC"):
        raise HTTPException(status_code=400, detail=f"unknown asset: {asset}")
    try:
        snap = signals.compute_vrp(asset_upper)
    except Exception as e:
        raise HTTPException(status_code=503,
                              detail=f"upstream data fetch failed: {e}")

    return JSONResponse({
        "ok":          True,
        "asset":       snap.asset,
        "vrp":         round(snap.vrp, 4),
        "regime":      snap.regime,
        "quiet":       snap.quiet,
        "inputs": {
            "dvol":        round(snap.dvol, 4),
            "rv_72h":      round(snap.rv_72h, 4),
            "rv_6h":       round(snap.rv_6h, 4),
            "spot_usd":    round(snap.spot, 2),
            "timestamp":   snap.timestamp_utc,
            "source":      "Deribit public API (DVOL + ETH-PERPETUAL/BTC-PERPETUAL OHLC)",
        },
        "methodology": f"{METHODOLOGY_BASE}/vrp-v1",
        "computed_at": int(time.time()),
        "cache_ttl_sec": signals.CACHE_TTL_SEC,
    })


_STARTED = time.time()
