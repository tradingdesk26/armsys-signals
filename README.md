# ARMS Signals API

**Derived market metrics for AI agents — pay-per-call via x402.**

Live: <https://regimeshift.xyz/api/>

## Why

Agents can read raw prices from any exchange for free. They **cannot** read
derived metrics — VRP, regime classification, IV skew anomaly, intraweek
seasonality bias — unless they build the research pipeline themselves.

ARMS Signals publishes these as paid HTTP endpoints. Pay per call in USDC
via the x402 protocol — no API keys, no subscriptions, no platform.

Reference customer: the [RegimeShift](https://github.com/tradingdesk26/vrp-agent)
autonomous agent that uses these same signals to trade delta-neutral VRP
across Hyperliquid + Base.

## Endpoints

| Path | Price | Description |
|------|-------|-------------|
| `GET /` | free | service status + x402 config |
| `GET /health` | free | liveness check |
| `GET /v1/asset/eth/vrp` | **$0.001** | ETH Volatility Risk Premium |
| `GET /v1/asset/btc/vrp` | **$0.001** | BTC Volatility Risk Premium |

VRP = DVOL − Parkinson realized vol over 72h. Positive = sell-vol
opportunity; negative = buy-vol opportunity.

## Payment

- **Protocol**: [x402](https://www.x402.org/) v2 — HTTP 402 + signed USDC payments
- **Network**: Base Sepolia (`eip155:84532`) for testnet trial
- **Token**: USDC at `0x036CbD53842c5426634e7929541eC2318f3dCF7e`
- **Pay to**: `0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B`
- **Facilitator**: `https://x402.org/facilitator` (default)

When Coinbase opens a Base-mainnet facilitator, the network flag flips
from Sepolia to mainnet and real USDC starts flowing.

## Try it

```bash
# Free — service info
curl https://regimeshift.xyz/api/

# Paid — returns 402 with payment requirements
curl -i https://regimeshift.xyz/api/v1/asset/eth/vrp

# Using x402 client (Python)
pip install 'x402[evm,fastapi]'
# ... client signs USDC payment, re-submits with X-Payment header
```

## Response shape

Every paid response includes the metric value plus a full audit trail:

```json
{
  "ok": true,
  "asset": "ETH",
  "vrp": 0.82,
  "regime": "MID",
  "quiet": true,
  "inputs": {
    "dvol": 52.8,
    "rv_72h": 51.98,
    "rv_6h": 30.85,
    "spot_usd": 2118.30,
    "timestamp": "2026-05-20T13:00:00+00:00",
    "source": "Deribit public API (DVOL + ETH-PERPETUAL OHLC)"
  },
  "methodology": "https://regimeshift.xyz/methodology/vrp-v1",
  "computed_at": 1779284275,
  "cache_ttl_sec": 60
}
```

Agents can re-fetch the source data, run the open methodology, and verify
the result. We don't ask agents to trust us — we make verification cheap.

## Stack

- **FastAPI** for the API server
- **x402 SDK (Python)** for the payment middleware
- **Deribit public API** for raw IV + OHLC
- **systemd** on a GCE VM behind Cloudflare + nginx

## Roadmap

- ✅ `/v1/asset/{eth,btc}/vrp` — live
- ⬜ `/v1/asset/{eth,btc}/regime` — LOW / MID / HIGH classification
- ⬜ `/v1/asset/{eth,btc}/iv-skew` — RR_10, Fly_10, term-structure
- ⬜ `/v1/asset/{eth,btc}/cross-event-now` — 240m/480m MA cross status + context
- ⬜ `/v1/asset/{eth,btc}/us-session-trigger` — long/short straddle window check
- ⬜ Open-source signals library on GitHub (formulas + tests)
- ⬜ Live performance feed (what's the reference agent doing right now)
- ⬜ Migrate to Base mainnet once Coinbase opens that facilitator

## Built for

[Agora Agents Hackathon](https://www.canteen.network/agora) — Canteen × Circle.
