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

Public base URL: `https://regimeshift.xyz/api/`

| Path | Price | Description |
|------|-------|-------------|
| `GET /` | free | service status + x402 config |
| `GET /health` | free | liveness check |
| `GET /stats` | free | request counter (paid calls + probes per endpoint) |
| `GET /v1/asset/eth/vrp` | **$0.001** | ETH Volatility Risk Premium |
| `GET /v1/asset/btc/vrp` | **$0.001** | BTC Volatility Risk Premium |

VRP = DVOL − Parkinson realized vol over 72h. Positive = sell-vol
opportunity; negative = buy-vol opportunity.

## Payment

- **Protocol**: [x402](https://www.x402.org/) v2 — HTTP 402 + signed USDC payments
- **Network**: **Base mainnet** (`eip155:8453`)
- **Token**: USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- **Pay to**: `0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B` (self-custodied
  agent wallet — same wallet the RegimeShift trading agent runs on,
  so paid USDC immediately becomes tradeable capital)
- **Facilitator**: `https://api.cdp.coinbase.com/platform/v2/x402`
  (Coinbase CDP, Ed25519-signed JWT auth)
- **Bazaar discovery**: `extensions.bazaar.{info,schema}` included in
  every 402 envelope so agentic.market and the CDP Bazaar index can
  catalog the endpoint automatically.

To run the server yourself, populate `.env` with:

```
CDP_API_KEY_ID=<uuid from portal.cdp.coinbase.com>
CDP_API_KEY_SECRET=<base64 Ed25519 private key, shown once at creation>
EVM_ADDRESS=0x...           # your own pay-to wallet
```

If those CDP variables are absent the server falls back to Base Sepolia
testnet + the default `x402.org` facilitator (good for local dev).

One important detail: the CDP facilitator rejects payloads where
`buyer == payTo` with `invalid_payload`. Use a distinct buyer wallet
(see `demo_client.py`).

## Try it

```bash
# Free — service info
curl https://regimeshift.xyz/api/

# Free — request counter
curl https://regimeshift.xyz/api/stats

# Paid — returns 402 with payment requirements (decoded from the
# base64 `payment-required` response header)
curl -i https://regimeshift.xyz/api/v1/asset/eth/vrp

# End-to-end paid call (Python) — see demo_client.py in this repo
EVM_PRIVATE_KEY=0x... python demo_client.py
```

A successful paid call returns HTTP 200 with the VRP payload and a
settlement receipt in the `payment-response` header that includes the
on-chain Base mainnet tx hash.

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

- **FastAPI** for the API server (uvicorn with `--proxy-headers --root-path /api`)
- **x402 SDK (Python)** for the payment middleware
- **Coinbase CDP SDK** for facilitator JWT auth
- **Deribit public API** for raw IV + OHLC
- **systemd** on a GCE VM behind Cloudflare + nginx
- Persistent request counter in JSON, exposed via `/stats`

## Files

```
app.py               FastAPI server + x402 paywall + bazaar discovery
signals.py           VRP computation from Deribit OHLC + DVOL
stats.py             Persistent request counter
demo_client.py       End-to-end paid call (x402HttpxClient over httpx)
arms-signals.service systemd unit
requirements.txt     pinned x402[evm,fastapi,extensions] + cdp-sdk + fastapi
```

## Roadmap

- ✅ `/v1/asset/{eth,btc}/vrp` — live on Base mainnet
- ✅ x402 paywall via Coinbase CDP facilitator — paid calls settle on-chain
- ✅ Bazaar discovery extension — listed on agentic.market
- ✅ Request counter visible on regimeshift.xyz dashboard
- ⬜ `/v1/asset/{eth,btc}/regime` — LOW / MID / HIGH classification
- ⬜ `/v1/asset/{eth,btc}/iv-skew` — RR_10, Fly_10, term-structure
- ⬜ `/v1/asset/{eth,btc}/cross-event-now` — 240m/480m MA cross status + context
- ⬜ `/v1/asset/{eth,btc}/us-session-trigger` — long/short straddle window check
- ⬜ Open-source signals library on GitHub (formulas + tests)
- ⬜ Live performance feed — what the reference agent is doing right now

## Built for

[Agora Agents Hackathon](https://thecanteenapp.com/) — Canteen × Circle.
