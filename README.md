# ARMS Signals API

**Derived market metrics + agent-native short-rate benchmark + bilateral RFQ marketplace — all pay-per-call via x402 on Base mainnet.**

Live: <https://regimeshift.xyz/api/>

## Why

Agents can read raw prices from any exchange for free. They **cannot** read:
- Derived volatility metrics (VRP, regime classification, IV skew)
- Decentralized USD short-rate benchmarks (agent-native SOFR equivalent)
- Risk-aware loan terms (max-safe LTV given variance + lender tolerance)
- Bilateral RFQ matching with EIP-712 signed quotes that an on-chain
  escrow contract can verify

ARMS Signals publishes all of these as paid HTTP endpoints. Pay per call
in USDC via the x402 protocol — no API keys, no subscriptions, no platform.

Reference customer: the [RegimeShift](https://github.com/tradingdesk26/vrp-agent)
autonomous agent that uses these same signals to trade delta-neutral VRP
across Hyperliquid + Base.

## Endpoints

Public base URL: `https://regimeshift.xyz/api/`

### Free service / status

| Path | Purpose |
|------|---------|
| `GET /` | service status + endpoint listing + x402 config |
| `GET /health` | liveness check |
| `GET /stats` | request counter (paid calls + probes per endpoint) |

### Paid data feeds (x402, tiered pricing)

| Path | Price | Description |
|------|-------|-------------|
| `GET /v1/asset/eth/vrp` | **$0.001** | ETH Volatility Risk Premium (DVOL − Parkinson RV 72h) |
| `GET /v1/asset/btc/vrp` | **$0.001** | BTC Volatility Risk Premium |
| `GET /v1/rate/sofr/usd?horizon=1h` | **$0.001** | Agent-SOFR USD short rate (multi-source weighted median + variance + regime premium) |
| `GET /v1/risk/max-ltv?asset=ETH&duration_sec=3600&max_default_prob=0.001` | **$0.001** | Max-safe LTV for collateralized loans (math max + regime cap) |

Pricing rationale (onboarding phase): all paid endpoints kept at **$0.001 per call** — the minimum amount agents will probe at when evaluating an unknown service. Friction-free for new agents to test our quality before relying on the data. Our primary revenue base is **loan-interest spread** in the clearinghouse (base + variance + regime premium, take ~5-10 bps) plus the **3% liquidator bounty** on `V4.defaultLoan()` — not endpoint micro-payments. Endpoint prices will be revisited once external paying-agent traffic stabilizes; eventual tier targets: VRP / max-LTV at CoinMarketCap-pro level ($0.005), Agent-SOFR at Messari-Enterprise tier ($0.10), signed loan quotes at $0.05 floor or 5 bps of principal.

### Also available over MCP

The same paid signals are exposed as a remote **Model Context Protocol** server at `https://mcp.regimeshift.xyz/` — streamable-HTTP `/mcp` + SSE `/sse`, paid per-tool via x402 on Base. Discoverable in the official MCP Registry as `xyz.regimeshift/mcp`.

### Inter-Agent Clearinghouse (free; settlement on-chain)

| Path | Purpose |
|------|---------|
| `POST /v1/intent/lend` | Submit lender intent to the order book (auto-fires matcher). Accepts optional `webhook_url` for push notification. |
| `POST /v1/intent/borrow` | Submit borrower intent (auto-fires matcher). Accepts optional `webhook_url`. |
| `GET /v1/intent/{intent_id}/match?wait=N` | **Long-poll** — holds connection up to 300s, returns immediately when match found. Use if you don't have a public webhook endpoint. |
| `GET /v1/intents/open` | List currently-open intents from both sides |
| `GET /v1/matches/recent` | Recent matches with full EIP-712 signed quote payloads ready for on-chain `originate()` |
| `GET /v1/active-loans` | All active loans with current on-chain LTV (Chainlink-priced) |
| `GET /v1/liquidatable-loans` | Loans where current LTV ≥ 95% and grace period passed — any agent can call `V4.liquidate()` for 3% bounty |

#### Match notifications (no polling required)

When you submit an intent, you get back an `intent_id`. To find out when it matches without polling, two options:

**Webhook (push)** — pass `webhook_url` at submit time:

```bash
POST /v1/intent/lend
{
  "wallet": "0xMyAgent...",
  "asset": "USDC",
  "amount": 50,
  "min_rate_bps": 480,
  "max_duration_sec": 14400,
  "webhook_url": "https://my-agent.example.com/match-callback"
}
```

When match fires, server `POST`s the signed quote payload to your URL within ~1s:

```json
{
  "event": "match_found",
  "match_id": "match_xyz...",
  "your_role": "lender",
  "your_intent_id": "lend_abc...",
  "quote": { /* full EIP-712 signed Quote, ready for originate() */ }
}
```

Best-effort: no retries, 5s timeout. Your handler must be idempotent (deduplicate by `match_id`).

**Long-poll (no public URL needed)** — single GET that holds up to 300s:

```bash
curl "https://regimeshift.xyz/api/v1/intent/lend_abc/match?wait=300"

# Returns immediately if matched, otherwise holds until match or timeout:
{
  "ok": true,
  "matched": true,
  "match_id": "match_xyz...",
  "elapsed_sec": 8.2,
  "quote": { /* full signed payload */ }
}
```

Works for local, serverless, or any agent that can hold a single TCP connection. Re-poll on timeout — effectively zero overhead between calls.

## On-chain settlement

The Inter-Agent Clearinghouse settles via custom escrow contracts on Base mainnet. Three audit rounds completed (10 → 3 → 1 → 0 findings); current production contract is V4.

**V4 (active — new quotes signed for this)**
- **InterAgentRepoV4** — [`0x9d3b61d13a839968ffad94a0eedf73153c2fb31c`](https://basescan.org/address/0x9d3b61d13a839968ffad94a0eedf73153c2fb31c)
- Functions: `originate()`, `repay()`, `defaultLoan()`, `liquidate()`, `currentLTV()` view
- Chainlink ETH/USD oracle for pre-expiry liquidation
- 95% LTV liquidation threshold, 3% liquidator bounty, 1% insurance fee, 60s grace period
- Aave-style default split: 3% bounty / 1% insurance / debt-equivalent to lender / excess refund to borrower
- Min loan duration: 120s; initial-LTV cap: 93% (enforced on-chain at origination)
- `whenNotPaused` removed from `repay()` (R2-#2) — owner cannot force borrower into default
- EIP-712 domain: `("InterAgentRepo", "4")`
- Foundry tests: passing under `via_ir`

**Retired contracts (oracleSigner rotated to `0x...dEaD`)**
- **InterAgentRepoV3** — [`0xFfca5d80c3413Bd5D17971550cCD615f57f22945`](https://basescan.org/address/0xFfca5d80c3413Bd5D17971550cCD615f57f22945) — retired after R3 cleanup
- **InterAgentRepoV2** — [`0x2bfE0f1142B04049d867389Bf91A84e498ED11E4`](https://basescan.org/address/0x2bfE0f1142B04049d867389Bf91A84e498ED11E4) — retired after R2
- **InterAgentRepo** (V1) — [`0xaea176DDa786c8B14802f92385749C7Cdf6C7400`](https://basescan.org/address/0xaea176DDa786c8B14802f92385749C7Cdf6C7400) — MVP no-liquidation reference

All retired contracts have `oracleSigner = 0x...dEaD`, so no new quotes can be signed for them.

V4:
- USDC principal + WETH collateral only (multi-asset is v2.0+)
- Source + Foundry tests: [`regimeshift-clearinghouse`](https://github.com/tradingdesk26/regimeshift-clearinghouse) (public)
- Audit reports: `audit/round1.md`, `audit/round2.md`, `audit/round3.md`

## What "Agent-SOFR" means

A decentralized benchmark rate for the agent economy — the LIBOR/SOFR of
machines. Refreshed every 60s, aggregated from 7 sources via weighted median
(WETH borrow source removed in v1.0.1 — see note below):

| Source | Weight | Notes |
|--------|--------|-------|
| Deribit ETH options PCP (30d) | 32% | Put-call parity → implied USD rate |
| Hyperliquid ETH-PERP funding | 22% | Annualized 1h funding rate |
| Aevo ETH options PCP | 11% | Cross-check on options markets |
| Deribit ETH futures basis (3m) | 10% | Cost-of-carry sanity check |
| Aave V3 Base USDC borrow | 10% | DeFi reference (capped — governance-set) |
| Compound Base USDC borrow | 5% | DeFi reference (capped — governance-set) |
| NY Fed SOFR 30d | 10% | TradFi macro anchor |

Note: WETH borrow rate **deliberately excluded** — it's the ETH lending market (interest paid in ETH), structurally unrelated to USDC short rate. We compute the rate at which agents borrow USD against ETH collateral, not the rate at which someone borrows ETH itself.

Total rate = `weighted_median(sources) + variance_premium + regime_adjustment`

Variance + regime calibration is **Barndorff-Nielsen–Shephard** jump decomposition: bipower variation (BV) and tripower quarticity (TQ) → Huang-Tauchen Z-statistic separates the continuous variance component `cv` from the jump component `j²`. Jump-weighting parameter λ is identified closed-form (5% margin over total integrated loss) → λ = 1.097. The 6-mode regime classifier is then calibrated on the BNS-decomposed series with 10% down-hysteresis.

Dataset: **444,219 5-min bars (~4.2 years) of Binance ETH/USDC 1-second tickers aggregated upward** (`arms/research/eth_jump_multihorizon.py`). Same math runs in the production [ARMSHookV3 Uniswap v4 hook](https://github.com/tradingdesk26/regimeshift-fx) deployed on Base mainnet.

Every API response carries the methodology page's `content_hash_sha256` + IPFS `ipfs_cid` — clients can independently verify the math they're paying for hasn't moved.

## Payment

- **Protocol**: [x402](https://www.x402.org/) v2 — HTTP 402 + signed USDC payments
- **Network**: **Base mainnet** (`eip155:8453`)
- **Token**: USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- **Pay to**: `0x82B17D0bb4De9ae6c3491257B60E8245e70acd7B` (self-custodied
  agent wallet — same wallet the RegimeShift trading agent runs on,
  so paid USDC immediately becomes tradeable capital)
- **Bazaar discovery**: `extensions.bazaar.{info,schema}` included in
  every 402 envelope so agentic.market and the x402 Bazaar index can
  catalog the endpoint automatically.

### Two-tier facilitator (resilience layer)

For a benchmark rate that other agents make trading decisions against,
"sometimes the paid endpoint is down" is not acceptable. So we architect
paid x402 settlement as a chain, not a single point of failure:

| Tier | URL | Role | Gas paid by |
|------|-----|------|-------------|
| **Primary** | `https://api.cdp.coinbase.com/platform/v2/x402` | Default for every paid call | Coinbase CDP relayer |
| **Fallback** | `http://127.0.0.1:8091` (self-hosted on the same VM) | Auto-engages on any primary failure | Our own relayer wallet (on Base mainnet) |

The middleware (`facilitator_failover.py`) wraps both clients. Every
`verify` / `settle` call tries the primary first; if it raises (timeout,
connection error, rate-limit, transient 5xx, anything), the call
transparently falls back to the secondary. External clients see exactly
one response — they don't notice failovers happened. A 200 OK with
`isValid=false` is treated as a legitimate rejection (signature really is
bad / authorization expired) and is NOT retried, because the secondary
would reject for the same reason.

The fallback is a ~150 LOC FastAPI service (`own_facilitator.py`) built
on the x402 SDK's `x402Facilitator` + `register_exact_evm_facilitator(eip155:8453)`
primitives — the same verify+settle code path CDP itself runs. Running
it on the same VM keeps roundtrip latency under 10 ms when fallback
engages, and means there is no path between "primary is down" and
"paid endpoints are unavailable".

Both tiers settle with real `USDC.transferWithAuthorization` (EIP-3009)
txs on Base mainnet — every paid 200 OK carries the on-chain tx hash in
the `payment-response` header, regardless of which tier handled it.

### Configuration

To run the server yourself, populate `.env` with:

```
# Primary facilitator — Coinbase CDP (auto-detected from these keys)
CDP_API_KEY_ID=<uuid from portal.cdp.coinbase.com>
CDP_API_KEY_SECRET=<base64 Ed25519 private key, shown once at creation>

# Fallback facilitator — local self-hosted (transparent failover)
FALLBACK_FACILITATOR_URL=http://127.0.0.1:8091

# Network + receiving wallet
EVM_NETWORK=eip155:8453                  # Base mainnet (CAIP-2)
EVM_ADDRESS=0x...                        # your pay-to wallet (receives paid USDC)

# For the clearinghouse routes
ORACLE_PRIVATE_KEY=0x...                 # signs loan quotes for InterAgentRepoV4
```

The self-hosted facilitator itself needs (`own_facilitator.py`):

```
EVM_PRIVATE_KEY=0x...                    # relayer key — pays gas during fallback
EVM_RPC_URL=https://base-mainnet...      # Base mainnet RPC endpoint
```

If `ORACLE_PRIVATE_KEY` is absent, only the rate/risk endpoints work
— `/v1/intent/*` and clearinghouse routes will fail to initialize.
If `FALLBACK_FACILITATOR_URL` is absent the server runs single-tier
(primary only). If `CDP_API_KEY_ID` is absent the primary becomes
whatever `FACILITATOR_URL` points at (defaults to Base Sepolia for dev).

## Try it

```bash
# Free — service info + endpoint listing
curl https://regimeshift.xyz/api/

# Free — request counter
curl https://regimeshift.xyz/api/stats

# Paid — returns 402 with payment requirements
curl -i https://regimeshift.xyz/api/v1/rate/sofr/usd?horizon=1h

# End-to-end paid call (Python)
EVM_PRIVATE_KEY=0x... \
  X402_URL=https://regimeshift.xyz/api/v1/rate/sofr/usd?horizon=1h \
  python demo_client.py

# Submit a lender intent (free; matches synchronously)
curl -X POST https://regimeshift.xyz/api/v1/intent/lend \
  -H "Content-Type: application/json" \
  -d '{
    "wallet": "0xYourAddr...",
    "asset": "USDC",
    "amount": 50.0,
    "max_duration_sec": 14400,
    "min_rate_bps": 450
  }'

# View matched quotes
curl https://regimeshift.xyz/api/v1/matches/recent
```

A successful paid call returns HTTP 200 with the payload and a
settlement receipt in the `payment-response` header that includes the
on-chain Base mainnet tx hash.

## Response shape — Agent-SOFR

```json
{
  "ok": true,
  "asset": "USD",
  "horizon": "1h",
  "rate": 4.4257,
  "decomposition": {
    "base_anchor": 4.1257,
    "variance_premium": 0.0,
    "regime_adjustment": 0.3
  },
  "variance": {
    "cv_per_bar": 3.82e-07,
    "j_squared_per_bar": 4.26e-06,
    "lambda_jump_weight": 1.097,
    "sigma_5min_bp": 22.48,
    "sigma_horizon_pct": 0.779
  },
  "regime": {
    "mode": "ELEVATED",
    "mode_index": 2,
    "thresholds_bp": {
      "p50": 14.21, "p65": 17.77, "p80": 23.25,
      "p93": 34.45, "p99": 62.93
    }
  },
  "sources": { /* per-source rate + weight + ok/error */ },
  "methodology": {
    "version": "agent-sofr-v1",
    "url": "https://regimeshift.xyz/methodology/agent-sofr-v1",
    "content_hash_sha256": "001dd476c5e14755617200899b1f7f1de4a6d54050c7eb3f6ffb3988c1a199fb",
    "ipfs_cid": "bafkreiaadxkhnrpbi5kwc4qargnr67y54stnkqcqy7vt6373hgemdimz7m",
    "ipfs_gateways": [
      "https://ipfs.io/ipfs/bafkreiaadxkhnrpbi5kwc4qargnr67y54stnkqcqy7vt6373hgemdimz7m",
      "https://dweb.link/ipfs/bafkreiaadxkhnrpbi5kwc4qargnr67y54stnkqcqy7vt6373hgemdimz7m"
    ],
    "calibration_source": "arms/research/eth_jump_multihorizon.py",
    "calibration_data": "444219 5-min bars (~4.2yr) Binance ETH/USDC, BNS jump decomposition (BV+TQ → Z-stat), lambda=1.097 closed-form",
    "verify": {
      "https": "curl https://regimeshift.xyz/methodology/agent-sofr-v1 | shasum -a 256",
      "ipfs": "curl -H 'Accept: application/vnd.ipld.raw' https://ipfs.io/ipfs/bafkrei... | shasum -a 256"
    }
  },
  "computed_at": 1779530967,
  "valid_until": 1779531027,
  "cache_ttl_sec": 60
}
```

## Response shape — match payload

`GET /v1/matches/recent` returns matched quotes ready for on-chain submission:

```json
{
  "match_id": "match_d422296808fcbec7",
  "lender_intent_id": "lend_...",
  "borrower_intent_id": "bor_...",
  "quote": {
    "quote": {
      "borrower": "0x...", "lender": "0x...",
      "principalToken": "0x833589fC...",  /* USDC on Base */
      "principalAmount": "50000000",       /* raw units */
      "collateralToken": "0x42000000...",  /* WETH on Base */
      "collateralAmount": "32051282051282056",
      "expiryTimestamp": 1779388183,
      "rateBps": 480,
      "nonce": "0x3d7c6fb1..."
    },
    "signature": "0x07bde95d...",          /* EIP-712 sig over the Quote struct */
    "decomposition": {
      "mode": "compute_collateral",
      "ltv": 0.75,                          /* LTV after regime cap */
      "regime": "HIGH",
      "variance_premium_bps": 0.0,
      "regime_premium_bps": 60.0,
      "base_anchor_pct": 4.1083
    },
    "contract": {
      "address": "0xaea176DDa786c8B14802f92385749C7Cdf6C7400",
      "chain_id": 8453
    }
  }
}
```

Either party (or anyone) can submit this directly to `InterAgentRepo.originate(quote, signature)` — both wallets need to have approved the contract for the relevant ERC-20 amounts beforehand.

## Stack

- **FastAPI** for the API server (uvicorn with `--proxy-headers --root-path /api`)
- **x402 SDK (Python) 2.x** for the payment middleware (server side)
- **Two-tier facilitator** — Coinbase CDP as primary (Coinbase pays relayer gas), self-hosted FastAPI service (`own_facilitator.py`, port 8091) as transparent fallback. Failover logic in `facilitator_failover.py` — wraps both `HTTPFacilitatorClient` instances behind the SDK's `FacilitatorClient` protocol so the rest of the stack doesn't know which tier handled a given call
- **Deribit / Hyperliquid / Aevo / Binance public APIs** for live market data
- **Aave V3 + Compound** on Base via direct eth_call to Pool contracts (Alchemy RPC)
- **systemd** on a GCE VM behind Cloudflare + nginx (two services: `arms-signals.service` for the API + `regimeshift-facilitator.service` for the local facilitator)
- **SQLite** for intent book + match persistence
- **scipy / numpy** for BNS jump decomposition + LTV math
- **eth-account / web3.py** for EIP-712 quote signing + facilitator settlement
- Persistent request counter in JSON, exposed via `/stats`

## Files

```
app.py                       FastAPI server + x402 paywall + clearinghouse routes
facilitator_failover.py      Two-tier FacilitatorClient (CDP primary + local fallback)
own_facilitator.py           Self-hosted x402 facilitator (verify + settle on Base mainnet)
signals.py                   VRP computation from Deribit OHLC + DVOL
stats.py                     Persistent request counter
demo_client.py               End-to-end paid call (x402 SDK over requests)
arms-signals.service         systemd unit (API on :8000)
regimeshift-facilitator.service  systemd unit (facilitator on :8091)
requirements.txt             pinned x402 + fastapi + scipy + eth-account + web3

oracle/                      Agent-SOFR oracle modules (calibration, regime,
                             variance, max_ltv, rate_aggregator, agent_sofr)
matcher/                     Intent book (SQLite) + matcher + quote engine (EIP-712)
```

## Roadmap

- ✅ `/v1/asset/{eth,btc}/vrp` — live on Base mainnet
- ✅ `/v1/rate/sofr/usd` — Agent-SOFR USD short-rate benchmark
- ✅ `/v1/risk/max-ltv` — max-safe LTV signal
- ✅ Inter-Agent Clearinghouse (`/intent/*`, `/matches/recent`) — live, end-to-end
- ✅ InterAgentRepoV4 deployed on Base + Foundry tests (8/8 + 15/15 ported)
- ✅ EIP-712 quote signing — on-chain `recoverSigner()` verified on V4
- ✅ **Two-tier x402 facilitator** on Base mainnet — Coinbase CDP primary + self-hosted fallback; every paid 200 settles via `transferWithAuthorization`, every tx visible on BaseScan
- ✅ Bazaar discovery extension — listed on agentic.market
- ✅ Request counter visible on regimeshift.xyz dashboard
- ✅ Live MVP demo loan executed on-chain (2026-05-22, $0.50 USDC, V4)
- ✅ Loan registry + Open Order Book panels live on regimeshift.xyz
- ✅ Methodology pages on `regimeshift.xyz/methodology/*` with IPFS pinning + SHA-256 in every API response
- ✅ Compound borrow rate read — live in aggregator
- ✅ BNS jump decomposition + λ=1.097 closed-form calibration on 444k bars
- ✅ Autonomous demo bot (D pays $0.001 USDC every 90 min for fresh SOFR — agents using our own data via x402)
- ⬜ `/v1/rate/sofr/{eur,eth}` — EUR + ETH rate variants
- ⬜ Multi-asset principal/collateral (V4 is USDC/WETH-only)

## Related repos

- [`tradingdesk26/regimeshift-clearinghouse`](https://github.com/tradingdesk26/regimeshift-clearinghouse) — InterAgentRepoV4 escrow contracts + audit rounds + thesis docs
- [`tradingdesk26/regimeshift-demo-activity`](https://github.com/tradingdesk26/regimeshift-demo-activity) — Autonomous bot that exercises these endpoints continuously; pays $0.001 USDC for Agent-SOFR every refresh via the paid x402 path
- [`tradingdesk26/regimeshift-agent-starter`](https://github.com/tradingdesk26/regimeshift-agent-starter) — Minimal starter kit (lender / borrower / liquidator / data-only roles) for new agents integrating against this API
- [`tradingdesk26/vrp-agent`](https://github.com/tradingdesk26/vrp-agent) — The autonomous portfolio agent (reference customer)
- [`tradingdesk26/regimeshift-fx`](https://github.com/tradingdesk26/regimeshift-fx) — EURC/USDC Uniswap v4 hook where the same regime classifier was first calibrated

## Built for

Originally built for the [Agora Agents Hackathon](https://thecanteenapp.com/) — Canteen × Circle; now live in production on Base mainnet.
