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

import signals

load_dotenv("/opt/arms-signals/.env")  # absolute path so it loads under systemd

METHODOLOGY_BASE = "https://regimeshift.xyz/methodology"

# x402 config. Mode is decided by presence of CDP_API_KEY_ID:
#   - With CDP creds → Coinbase CDP facilitator on Base MAINNET (real USDC).
#   - Without        → default x402.org facilitator on Base Sepolia (testnet).
EVM_ADDRESS = os.getenv("EVM_ADDRESS", "0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B")
CDP_API_KEY_ID     = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")

if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    EVM_NETWORK: Network = os.getenv("EVM_NETWORK", "eip155:8453")  # Base mainnet
    # CDP facilitator base path is /platform/v2/x402 (no `/facilitator`
    # suffix — that variant returns 404). Endpoints are /verify,
    # /settle, /supported, /discovery/resources.
    FACILITATOR_URL = os.getenv(
        "FACILITATOR_URL",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    )
    USE_CDP = True
else:
    EVM_NETWORK = os.getenv("EVM_NETWORK", "eip155:84532")  # Base Sepolia
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
x402_routes = {
    "GET /v1/asset/*": RouteConfig(
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
            "Volatility Risk Premium — DVOL minus Parkinson realized vol (72h) "
            "for ETH or BTC. Positive = sell-vol opportunity, negative = "
            "buy-vol. Response includes regime classification, raw inputs, "
            "and open methodology URL for audit."
        ),
        extensions=_VRP_DISCOVERY,
    ),
}
app.add_middleware(PaymentMiddlewareASGI,
                    routes=x402_routes, server=x402_server)


@app.get("/")
def root():
    return {
        "service":   "ARMS Signals API",
        "version":   "0.1.0",
        "status":    "ok",
        "endpoints": [
            "/v1/asset/eth/vrp",
            "/v1/asset/btc/vrp",
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
