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
from fastapi.responses import JSONResponse, PlainTextResponse

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

# Inter-Agent Clearinghouse: intent book, matcher, quote engine, liquidation monitor
from matcher.intent_book import IntentBook
from matcher.quote_engine import QuoteEngine
from matcher.matcher import Matcher
from matcher.liquidation_monitor import (
    scan_liquidatable_loans, scan_all_active_loans,
)

load_dotenv("/opt/arms-signals/.env")  # absolute path so it loads under systemd

stats = StatsCounter()

# Inter-Agent Clearinghouse singletons. Lazily initialized so the app can
# start even if ORACLE_PRIVATE_KEY isn't set — only /v1/quote and /v1/intent/*
# routes will fail in that case, the rate/risk endpoints still work.
_intent_book: IntentBook | None = None
_quote_engine: QuoteEngine | None = None
_matcher: Matcher | None = None


def _get_intent_book() -> IntentBook:
    global _intent_book
    if _intent_book is None:
        _intent_book = IntentBook()
    return _intent_book


def _get_quote_engine() -> QuoteEngine:
    global _quote_engine
    if _quote_engine is None:
        _quote_engine = QuoteEngine()  # reads ORACLE_PRIVATE_KEY from env
    return _quote_engine


def _get_matcher() -> Matcher:
    global _matcher
    if _matcher is None:
        _matcher = Matcher(_get_intent_book(), _get_quote_engine())
    return _matcher

METHODOLOGY_BASE = "https://regimeshift.xyz/methodology"

# x402 facilitator config — two-tier with transparent failover.
#
# PRIMARY:    Coinbase CDP if CDP_API_KEY_ID is set (default), otherwise the
#             URL in FACILITATOR_URL (own/x402.org).
# FALLBACK:   Optional, set via FALLBACK_FACILITATOR_URL. When configured,
#             every verify/settle call tries the primary first and
#             transparently fails over to the fallback on any exception.
#             This keeps paid endpoints serving even if the primary is
#             rate-limiting, 5xx-ing, or briefly unreachable.
EVM_ADDRESS = os.getenv("EVM_ADDRESS", "0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B")
CDP_API_KEY_ID     = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")

if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    EVM_NETWORK: Network = os.getenv("EVM_NETWORK", "eip155:8453")  # Base mainnet, CAIP-2
    FACILITATOR_URL = os.getenv(
        "FACILITATOR_URL",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    )
    USE_CDP = True
else:
    EVM_NETWORK = os.getenv("EVM_NETWORK", "eip155:84532")  # Base Sepolia v2
    FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
    USE_CDP = False

# Optional fallback — typically our own facilitator on the same VM
FALLBACK_FACILITATOR_URL = os.getenv("FALLBACK_FACILITATOR_URL", "").strip()


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

_primary_facilitator = HTTPFacilitatorClient(FacilitatorConfig(**_fac_config_kwargs))

# Wire up the two-tier facilitator if a fallback URL is configured and is
# distinct from the primary. Falls back transparently on any primary failure
# (verify or settle). External clients see no behavioural difference.
if (
    FALLBACK_FACILITATOR_URL
    and FALLBACK_FACILITATOR_URL.rstrip("/") != FACILITATOR_URL.rstrip("/")
):
    from facilitator_failover import FallbackFacilitatorClient
    _fallback_facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(url=FALLBACK_FACILITATOR_URL)
    )
    facilitator = FallbackFacilitatorClient(_primary_facilitator, _fallback_facilitator)
    print(
        f"[x402] facilitator (hybrid):  primary={FACILITATOR_URL}  "
        f"fallback={FALLBACK_FACILITATOR_URL}"
    )
else:
    facilitator = _primary_facilitator
    print(f"[x402] facilitator (single): {FACILITATOR_URL}")

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
                price="$0.005",
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
    input={"horizon": "1h"},  # ← non-empty so CDP's Bazaar accepts the query param schema
    output=OutputConfig(example=_SOFR_SAMPLE_OUTPUT, schema=_SOFR_OUTPUT_SCHEMA),
)


def _sofr_route(asset: str) -> RouteConfig:
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price="$0.10",
                network=EVM_NETWORK,
            ),
        ],
        mime_type="application/json",
        # Description kept under 500 chars — CDP facilitator's JSON-schema
        # validator hard-rejects payloads with description > 500. Detailed
        # source list + weights are available in the response (`sources`
        # field) and on the IPFS-pinned methodology page.
        description=(
            f"Agent-SOFR — decentralized {asset} short-rate benchmark for AI "
            f"agents. Weighted-median of 7 sources + variance + regime "
            f"premiums. BNS-calibrated 6-mode classifier on 444k ETH/USDC "
            f"5-min bars (λ=1.097 closed-form). Open + IPFS-pinned "
            f"methodology: agent-sofr-v1."
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
    input={"asset": "ETH", "duration_sec": 3600, "max_default_prob": 0.001},
    output=OutputConfig(example=_MAX_LTV_SAMPLE, schema=_MAX_LTV_SCHEMA),
)


def _max_ltv_route() -> RouteConfig:
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price="$0.005",
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
        "endpoints": {
            "data": [
                "/v1/asset/eth/vrp",
                "/v1/asset/btc/vrp",
                "/v1/rate/sofr/usd",
                "/v1/risk/max-ltv",
            ],
            "clearinghouse": [
                "POST /v1/intent/lend           (accepts webhook_url for push notification)",
                "POST /v1/intent/borrow         (accepts webhook_url for push notification)",
                "GET /v1/intent/{id}/match?wait=N   (long-poll, max wait=300s)",
                "GET /v1/intents/open",
                "GET /v1/matches/recent",
                "GET /v1/active-loans",
                "GET /v1/liquidatable-loans",
                "GET /v1/loans/registry         (on-chain V4 event scan — full loan lifecycle with tx hashes)",
            ],
        },
        "contracts": {
            "InterAgentRepoV1": "0xaea176DDa786c8B14802f92385749C7Cdf6C7400 (MVP demo)",
            "InterAgentRepoV2": "0x2bfE0f1142B04049d867389Bf91A84e498ED11E4 (RETIRED — oracleSigner=0x...dEaD)",
            "InterAgentRepoV3": "0xFfca5d80c3413Bd5D17971550cCD615f57f22945 (RETIRED per R3-#1 — oracleSigner=0x...dEaD)",
            "InterAgentRepoV4": "0x9d3b61d13a839968ffad94a0eedf73153c2fb31c",
            "active": "V4",
            "chain": "Base mainnet (8453)",
            "audit_status": "rounds 1 + 2 + 3 fully addressed: V4 active, V3 + V2 retired (oracleSigner=0x...dEaD). Trajectory: 10 → 3 → 1 findings.",
        },
        "dashboard": "https://regimeshift.xyz",
        "github":    "https://github.com/tradingdesk26",
        "x402": {
            "status":     "live",
            "network":    EVM_NETWORK,
            "pay_to":     EVM_ADDRESS,
            "facilitator": FACILITATOR_URL,
            "pricing": {
                "VRP (ETH/BTC)":           "$0.005",
                "Agent-SOFR USD":          "$0.10",
                "Max-LTV risk":            "$0.005",
                "Quote (signed, future)":  "$0.05 flat or 5 bps of principal",
            },
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "uptime_sec": int(time.time() - _STARTED)}


@app.get("/quickstart.txt", response_class=PlainTextResponse)
def quickstart():
    """Plain-text quickstart for x402 catalog browsers and AI agents."""
    return """RegimeShift Clearinghouse — x402 Quickstart
=============================================

What this is
------------
Paid x402 data service + on-chain RFQ capital market for AI agents,
live on Base mainnet. Agents pay tiny USDC amounts via x402 to read
benchmark rates + risk parameters, then submit free intents to an
order book where the matcher pairs them into atomic on-chain loans.

Paid data endpoints (settle via x402)
-------------------------------------
GET  /v1/rate/sofr/usd?horizon=1h            $0.10  Agent-SOFR (decentralized USD short-rate benchmark, 7 sources + variance + regime premiums, BNS-calibrated)
GET  /v1/risk/max-ltv?asset=ETH&...          $0.005 Max safe loan-to-value given variance + lender's default tolerance
GET  /v1/asset/eth/vrp                       $0.005 ETH volatility risk premium (DVOL − Parkinson RV 72h)
GET  /v1/asset/btc/vrp                       $0.005 BTC volatility risk premium

Free endpoints (intent submission + chain reads)
------------------------------------------------
POST /v1/intent/lend                         Submit lender intent (rate, amount, max duration)
POST /v1/intent/borrow                       Submit borrower intent (rate ceiling, principal, collateral)
GET  /v1/intent/{id}/match?wait=300          Long-poll for match (holds up to 300s)
GET  /v1/intents/open                        Current order book (both sides)
GET  /v1/matches/recent                      Recent EIP-712 signed quotes ready for V4.originate()
GET  /v1/loans/registry                      Full on-chain loan lifecycle (open / repaid / defaulted)
GET  /v1/active-loans                        Currently active loans
GET  /v1/liquidatable-loans                  Loans past LTV threshold (3% bounty)
GET  /stats                                  Per-endpoint counters (402 vs 200)
GET  /openapi.json                           Machine-readable API spec

Network
-------
Base mainnet (eip155:8453)
USDC settlement token: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
Pay to: 0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B
Active escrow contract: InterAgentRepoV4 0x9d3b61d13a839968ffad94a0eedf73153c2fb31c

Facilitator
-----------
Two-tier: Coinbase CDP primary + self-hosted fallback. Drop-in for
any x402 server — standalone repo: github.com/tradingdesk26/arc-x402-facilitator-failover

How to pay (x402 SDK 2.x, Python)
---------------------------------
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.http.clients import x402_http_adapter
    from eth_account import Account
    import requests

    acct   = Account.from_key("0xYOUR_PK")
    client = x402ClientSync()
    client.register("eip155:8453", ExactEvmScheme(signer=acct))
    s = requests.Session()
    s.mount("https://", x402_http_adapter(client))

    r = s.get("https://regimeshift.xyz/api/v1/rate/sofr/usd?horizon=1h")
    print(r.json())   # rate, decomposition, regime, sources, methodology

Methodology (audited)
---------------------
agent-sofr-v1: Barndorff-Nielsen-Shephard jump decomposition, lambda=1.097
closed-form on 444k 5-min bars of ETH/USDC. IPFS-pinned:
https://regimeshift.xyz/methodology/agent-sofr-v1
SHA-256 of the methodology page is included in every paid response so
clients can verify the math hasn't moved.

Code (all MIT, 6 public repos)
------------------------------
https://github.com/tradingdesk26/armsys-signals                  this API server + x402 paywall
https://github.com/tradingdesk26/regimeshift-clearinghouse       InterAgentRepoV4 + audit reports + thesis docs
https://github.com/tradingdesk26/regimeshift-agent-starter       4-role agent template (lender/borrower/liquidator/data-only)
https://github.com/tradingdesk26/regimeshift-demo-activity       autonomous demo bot (3-wallet role separation)
https://github.com/tradingdesk26/arc-x402-facilitator-failover   standalone two-tier facilitator (Arc OSS)
https://github.com/tradingdesk26/regimeshift-website             landing + methodology pages

Live dashboard: https://regimeshift.xyz
"""


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


# ─── Inter-Agent Clearinghouse endpoints (free; settlement on-chain) ──

@app.post("/v1/intent/lend")
async def post_intent_lend(payload: dict):
    """
    Submit a lender intent to the order book. Free (matching runs synchronously
    on each new intent; matched quote payload available via /v1/matches/recent).
    """
    required = ["wallet", "asset", "amount", "max_duration_sec", "min_rate_bps"]
    missing = [k for k in required if k not in payload]
    if missing:
        raise HTTPException(400, f"missing fields: {missing}")
    intent = _get_intent_book().add_lender(payload)
    # Run matcher synchronously
    match = None
    try:
        match = _get_matcher().find_match()
    except Exception:
        pass
    return JSONResponse({
        "ok": True,
        "intent_id": intent.intent_id,
        "matched": match.match_id if match else None,
    })


@app.post("/v1/intent/borrow")
async def post_intent_borrow(payload: dict):
    """Submit a borrower intent. Returns match_id if compatible lender found."""
    required = ["wallet", "principal_asset", "principal_amount",
                "collateral_asset", "collateral_amount_max",
                "duration_sec", "max_rate_bps"]
    missing = [k for k in required if k not in payload]
    if missing:
        raise HTTPException(400, f"missing fields: {missing}")
    intent = _get_intent_book().add_borrower(payload)
    match = None
    try:
        match = _get_matcher().find_match()
    except Exception:
        pass
    return JSONResponse({
        "ok": True,
        "intent_id": intent.intent_id,
        "matched": match.match_id if match else None,
    })


@app.get("/v1/intents/open")
def get_open_intents():
    """Return all currently-open intents from both sides."""
    book = _get_intent_book()
    lenders = book.open_lenders()
    borrowers = book.open_borrowers()
    return {
        "ok": True,
        "lenders": [
            {
                "intent_id": l.intent_id, "wallet": l.wallet,
                "asset": l.asset, "amount": l.amount,
                "max_duration_sec": l.max_duration_sec,
                "min_rate_bps": l.min_rate_bps,
                "max_default_prob": l.max_default_prob,
                "expires_at": l.expires_at,
            } for l in lenders
        ],
        "borrowers": [
            {
                "intent_id": b.intent_id, "wallet": b.wallet,
                "principal_asset": b.principal_asset,
                "principal_amount": b.principal_amount,
                "collateral_asset": b.collateral_asset,
                "collateral_amount_max": b.collateral_amount_max,
                "duration_sec": b.duration_sec,
                "max_rate_bps": b.max_rate_bps,
                "expires_at": b.expires_at,
            } for b in borrowers
        ],
    }


@app.get("/v1/liquidatable-loans")
def get_liquidatable_loans():
    """
    Return all currently-active loans whose on-chain LTV ≥ 95% (liquidation
    threshold) AND grace period has passed. Any agent can call
    InterAgentRepoV4.liquidate(loanId) to claim the 3% bounty.

    Read-only — does NOT execute liquidations. Free endpoint (encourages
    monitoring + competition between liquidators).
    """
    try:
        loans = scan_liquidatable_loans(_get_intent_book(), limit=100)
        return {
            "ok": True,
            "contract": "0x9d3b61d13a839968ffad94a0eedf73153c2fb31c",
            "chain_id": 8453,
            "liquidation_ltv_threshold_bps": 9500,
            "liquidator_bounty_bps": 300,
            "insurance_fee_bps": 100,
            "loans": [loan.to_dict() for loan in loans],
        }
    except Exception as e:
        raise HTTPException(503, f"liquidation monitor failed: {e}")


@app.get("/v1/active-loans")
def get_active_loans():
    """
    Return all currently-active loans with current LTV (whether liquidatable
    or not). Useful for dashboard / lender position monitoring.
    """
    try:
        loans = scan_all_active_loans(_get_intent_book(), limit=100)
        return {
            "ok": True,
            "contract": "0x9d3b61d13a839968ffad94a0eedf73153c2fb31c",
            "chain_id": 8453,
            "loans": [loan.to_dict() for loan in loans],
        }
    except Exception as e:
        raise HTTPException(503, f"active loans scan failed: {e}")


@app.get("/v1/loans/registry")
def get_loan_registry(limit: int = 50):
    """
    Full lifecycle of every V4 loan reconstructed from on-chain event logs.

    Returns the last `limit` originated loans (within ~14h lookback), each
    enriched with its close-event tx hash + final status. Cached for 30s.

    Each record contains: loan_id, status (active/repaid/defaulted/liquidated),
    borrower, lender, principal, collateral, rate, LTV, regime data (via match),
    and on-chain tx hashes (originate + close).

    Useful for public transparency dashboards.
    """
    try:
        from oracle.loan_history import build_loan_registry
        loans = build_loan_registry(limit=min(max(limit, 1), 100))
        return {
            "ok": True,
            "contract": "0x9d3b61d13a839968ffad94a0eedf73153c2fb31c",
            "chain_id": 8453,
            "count": len(loans),
            "loans": loans,
        }
    except Exception as e:
        raise HTTPException(503, f"loan registry build failed: {e}")


@app.get("/v1/intent/{intent_id}/match")
async def long_poll_intent_match(intent_id: str, wait: int = 60):
    """
    Long-poll: returns immediately if intent is already matched, otherwise
    holds the connection up to `wait` seconds (max 300) and returns as soon
    as a match is found. Use for agents that don't have a public webhook URL.

    Response:
      - matched=true + match_id + full quote when matched
      - matched=false + timeout_sec when wait elapses without match
      - 404 if intent_id unknown
    """
    import asyncio
    book = _get_intent_book()

    # Validate intent exists
    intent = book.get_intent(intent_id)
    if intent is None:
        raise HTTPException(404, f"intent {intent_id} not found")

    wait = max(0, min(wait, 300))
    start = time.time()
    poll_interval = 1.0
    deadline = start + wait

    while True:
        match = book.find_match_for_intent(intent_id)
        if match is not None:
            import json as _json
            try:
                quote = _json.loads(match["quote_payload"])
            except Exception:
                quote = None
            return JSONResponse({
                "ok": True,
                "intent_id": intent_id,
                "matched": True,
                "match_id": match["match_id"],
                "quote": quote,
                "elapsed_sec": round(time.time() - start, 2),
            })

        # If wait=0 or deadline reached, return matched=false
        if time.time() >= deadline:
            return JSONResponse({
                "ok": True,
                "intent_id": intent_id,
                "matched": False,
                "timeout_sec": wait,
                "elapsed_sec": round(time.time() - start, 2),
                "hint": "Re-poll with the same intent_id, or submit with webhook_url for push notifications.",
            })

        await asyncio.sleep(poll_interval)


@app.get("/v1/matches/recent")
def get_recent_matches(limit: int = 20):
    """Recent matches with full quote payload (signature, decomposition, contract address)."""
    import json as _json
    matches = _get_intent_book().recent_matches(limit=limit)
    out = []
    for m in matches:
        try:
            quote = _json.loads(m["quote_payload"])
        except Exception:
            quote = None
        out.append({
            "match_id": m["match_id"],
            "lender_intent_id": m["lender_intent_id"],
            "borrower_intent_id": m["borrower_intent_id"],
            "quote": quote,
            "created_at": m["created_at"],
        })
    return {"ok": True, "matches": out}


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
