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
| `GET /v1/asset/eth/vrp` | **$0.005** | ETH Volatility Risk Premium (DVOL − Parkinson RV 72h) |
| `GET /v1/asset/btc/vrp` | **$0.005** | BTC Volatility Risk Premium |
| `GET /v1/rate/sofr/usd?horizon=1h` | **$0.10** | Agent-SOFR USD short rate (multi-source weighted median + variance + regime premium). Messari Enterprise tier — unique product, no equivalent elsewhere. |
| `GET /v1/risk/max-ltv?asset=ETH&duration_sec=3600&max_default_prob=0.001` | **$0.005** | Max-safe LTV for collateralized loans (math max + regime cap) |

Pricing rationale: VRP and max-LTV are commodity signal endpoints competitive with CoinMarketCap pro tier ($0.005). Agent-SOFR is a category-defining product — the only on-chain decentralized USD benchmark rate aggregated from 8 manipulation-resistant sources — priced at Messari Enterprise tier ($0.10) accordingly. Signed loan quotes (forthcoming) will be the highest tier at flat $0.05 floor or 5 bps of principal, whichever larger.

### Inter-Agent Clearinghouse (free; settlement on-chain)

| Path | Purpose |
|------|---------|
| `POST /v1/intent/lend` | Submit lender intent to the order book (auto-fires matcher) |
| `POST /v1/intent/borrow` | Submit borrower intent (auto-fires matcher) |
| `GET /v1/intents/open` | List currently-open intents from both sides |
| `GET /v1/matches/recent` | Recent matches with full EIP-712 signed quote payloads ready for on-chain `originate()` |
| `GET /v1/active-loans` | All active loans with current on-chain LTV (Chainlink-priced) |
| `GET /v1/liquidatable-loans` | Loans where current LTV ≥ 95% and grace period passed — any agent can call `V2.liquidate()` for 3% bounty |

## On-chain settlement

The Inter-Agent Clearinghouse settles via custom escrow contracts on Base mainnet. Two versions are deployed:

**V2 (active — new quotes signed for this)**
- **InterAgentRepoV2** — [`0x2bfE0f1142B04049d867389Bf91A84e498ED11E4`](https://basescan.org/address/0x2bfE0f1142B04049d867389Bf91A84e498ED11E4)
- Deploy tx: [`0xad3fdca2...3e9bab0a`](https://basescan.org/tx/0xad3fdca2013de1a995dd3bc5778d539d6e443feec07aaff149eb291b3e9bab0a)
- Functions: `originate()`, `repay()`, `defaultLoan()`, **`liquidate()`** (new), `currentLTV()` view
- Chainlink ETH/USD oracle for pre-expiry liquidation
- 95% LTV liquidation threshold, 3% liquidator bounty, 1% insurance fee, 60s grace period
- EIP-712 domain: `("InterAgentRepo", "2")`
- Foundry tests: 14/14 passing

**V1 (kept live — MVP-no-liquidation reference)**
- **InterAgentRepo** — [`0xaea176DDa786c8B14802f92385749C7Cdf6C7400`](https://basescan.org/address/0xaea176DDa786c8B14802f92385749C7Cdf6C7400)
- Deploy tx: [`0xf2344c9c...ba2698`](https://basescan.org/tx/0xf2344c9cd8a90c9371d990cc8420bbf839ac14fb9fb099f8c5465f0354ba2698)
- Same `originate/repay/defaultLoan` lifecycle, no liquidate
- EIP-712 domain: `("InterAgentRepo", "1")` — V1 quotes can't replay against V2

Both contracts:
- MVP cap: $50 USDC principal per loan
- USDC principal + WETH collateral only (multi-asset is v2.0+)
- Source + Foundry tests: [`regimeshift-clearinghouse`](https://github.com/tradingdesk26/regimeshift-clearinghouse) (private during hackathon)

## What "Agent-SOFR" means

A decentralized benchmark rate for the agent economy — the LIBOR/SOFR of
machines. Refreshed every 60s, aggregated from 8 sources:

| Source | Weight | Notes |
|--------|--------|-------|
| Deribit ETH options PCP (30d) | 30% | Put-call parity → implied USD rate |
| Hyperliquid ETH-PERP funding | 20% | Annualized 1h funding rate |
| Aevo ETH options PCP | 10% | Cross-check on options markets |
| Deribit ETH futures basis (3m) | 10% | Cost-of-carry sanity check |
| Aave V3 Base USDC borrow | 10% | DeFi reference (capped — governance-set) |
| Aave V3 Base WETH borrow | 5% | Same |
| Compound Base USDC | 5% | TODO — not yet implemented |
| NY Fed SOFR 30d | 10% | TradFi macro anchor |

Total rate = `weighted_median(sources) + variance_premium + regime_adjustment`

Calibration inherited from the production [ARMSHookV3 Uniswap v4 hook](https://github.com/tradingdesk26/regimeshift-fx) — 730 days of ETH/USDT 5-min bars, 210k observations, 6-mode regime classifier with 10% down-hysteresis. **Same math as the hook deployed on Base mainnet for weeks.**

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
EVM_ADDRESS=0x...               # your own pay-to wallet
ORACLE_PRIVATE_KEY=0x...        # keypair authorized to sign loan quotes
```

If CDP credentials are absent the server falls back to Base Sepolia
testnet + the default `x402.org` facilitator (good for local dev).
If `ORACLE_PRIVATE_KEY` is absent, only the rate/risk endpoints work
— `/quote` and `/intent/*` will fail to initialize.

One important detail: the CDP facilitator rejects payloads where
`buyer == payTo` with `invalid_payload`. Use a distinct buyer wallet
(see `demo_client.py`).

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
  "rate": 4.7233,
  "decomposition": {
    "base_anchor": 4.1233,
    "variance_premium": 0.0,
    "regime_adjustment": 0.6
  },
  "variance": {
    "cv_per_bar": 1.586e-06,
    "j_squared_per_bar": 1.508e-05,
    "lambda_jump_weight": 1.097,
    "sigma_5min_bp": 42.58,
    "sigma_horizon_pct": 1.475
  },
  "regime": {
    "mode": "HIGH",
    "mode_index": 4,
    "thresholds_bp": {
      "p50": 14.21, "p65": 17.77, "p80": 23.25,
      "p93": 34.45, "p99": 62.93
    }
  },
  "sources": { /* per-source rate + weight + ok/error */ },
  "methodology": {
    "version": "agent-sofr-v1",
    "url": "https://regimeshift.xyz/methodology/agent-sofr-v1",
    "calibration_source": "arms/research/round25_calibration.csv",
    "calibration_data": "210228 ETH/USDT 5-min bars (2024-04-26 → 2026-04-26)"
  },
  "computed_at": 1779383267,
  "valid_until": 1779383327,
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
- **x402 SDK (Python)** for the payment middleware
- **Coinbase CDP SDK** for facilitator JWT auth
- **Deribit / Hyperliquid / Aevo / Binance public APIs** for live market data
- **Aave V3 + Compound** on Base via direct eth_call to Pool contracts (Alchemy RPC)
- **systemd** on a GCE VM behind Cloudflare + nginx
- **SQLite** for intent book + match persistence
- **scipy / numpy** for variance + LTV math
- **eth-account** for EIP-712 quote signing
- Persistent request counter in JSON, exposed via `/stats`

## Files

```
app.py                FastAPI server + x402 paywall + clearinghouse routes
signals.py            VRP computation from Deribit OHLC + DVOL
stats.py              Persistent request counter
demo_client.py        End-to-end paid call (x402HttpxClient over httpx)
arms-signals.service  systemd unit
requirements.txt      pinned x402 + cdp-sdk + fastapi + scipy + eth-account

oracle/               Agent-SOFR oracle modules (calibration, regime, variance,
                      max_ltv, rate_aggregator, agent_sofr)
matcher/              Intent book (SQLite) + matcher + quote engine (EIP-712)
```

## Roadmap

- ✅ `/v1/asset/{eth,btc}/vrp` — live on Base mainnet
- ✅ `/v1/rate/sofr/usd` — Agent-SOFR USD short-rate benchmark
- ✅ `/v1/risk/max-ltv` — max-safe LTV signal
- ✅ Inter-Agent Clearinghouse (`/intent/*`, `/matches/recent`) — live, end-to-end
- ✅ InterAgentRepo.sol deployed on Base + Foundry tests (10/10)
- ✅ EIP-712 quote signing — on-chain `recoverSigner()` verified
- ✅ x402 paywall via Coinbase CDP facilitator — paid calls settle on-chain
- ✅ Bazaar discovery extension — listed on agentic.market
- ✅ Request counter visible on regimeshift.xyz dashboard
- ⬜ Live demo loan executed on-chain ($5-10 between funded wallets)
- ⬜ Dashboard panel showing live intents + recent matches
- ⬜ Methodology pages on `regimeshift.xyz/methodology/*` with IPFS pinning
- ⬜ `/v1/rate/sofr/{eur,eth}` — EUR + ETH rate variants
- ⬜ Compound borrow rate read (currently TODO in rate aggregator)

## Built for

[Agora Agents Hackathon](https://thecanteenapp.com/) — Canteen × Circle.
