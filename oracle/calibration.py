"""
Production calibration constants for the Agent-SOFR oracle.

All values inherited from ARMSHookV3 production hook deployed on Base mainnet.
Calibration data: 730 days of ETH/USDT 5-minute bars (210,228 observations).

DO NOT modify these values without bumping the methodology version
(agent-sofr-v1 → agent-sofr-v2) and re-pinning the IPFS doc. Any change
must be auditable from the API response's methodology_hash.

Sources:
    /Users/dz/arms/src/bench/FeeFormulaV2.sol       — Solidity reference
    /Users/dz/arms/src/bench/RegimeCaps.sol         — Fee schedule (basis for risk premium curve)
    /Users/dz/arms/research/round25_calibration.csv — σ thresholds + time-share
    /Users/dz/arms/research/cooldown_matrix.py      — Hysteresis tuning
    /Users/dz/arms/research/percentile_grid.py      — σ percentile calculation
    /Users/dz/arms/research/ethusdt_5m.parquet      — Raw OHLCV data

Calibration period: 2024-04-26 → 2026-04-26
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


# ─────────────────────────────────────────────────────────────────────────────
# Methodology version + provenance
# ─────────────────────────────────────────────────────────────────────────────

METHODOLOGY_VERSION: Final[str] = "agent-sofr-v1"

CALIBRATION_SOURCE: Final[str] = "arms/research/round25_calibration.csv"

CALIBRATION_DATA: Final[str] = (
    "210228 ETH/USDT 5-min bars (2024-04-26 → 2026-04-26, "
    "source Binance public klines API via vol_calibration.py)"
)

# Bumped when any constant below changes.
# This hash is included in every API response so agents can verify
# they got rates computed under a specific methodology version.
METHODOLOGY_IPFS_HASH: Final[str] = ""  # populated post-pin


# ─────────────────────────────────────────────────────────────────────────────
# Sigma thresholds — 5-minute realized volatility percentiles
#
# These cut the rolling 1h σ distribution into 6 regime bands.
# Source: research/percentile_grid.py output, captured in
# research/round25_calibration.csv
# ─────────────────────────────────────────────────────────────────────────────

# σ values in fractional units (1.0 = 100%, 0.001 = 10 bp)
# Match the WAD representation in ARMSHookV3.sol verbatim.
SIGMA_P50: Final[float] = 1.421454920550197214e-03  # RESTING ↔ LOW    (14.21 bp)
SIGMA_P65: Final[float] = 1.776581506280669681e-03  # LOW ↔ NORMAL     (17.77 bp)
SIGMA_P80: Final[float] = 2.325150405799026546e-03  # NORMAL ↔ ELEVATED (23.25 bp)
SIGMA_P93: Final[float] = 3.444920341599264253e-03  # ELEVATED ↔ HIGH   (34.45 bp)
SIGMA_P99: Final[float] = 6.293340499719166786e-03  # HIGH ↔ EXTREME    (62.93 bp)

# Ordered tuple for iteration. Index i separates mode i from mode i+1.
SIGMA_CUTS: Final[tuple[float, ...]] = (
    SIGMA_P50, SIGMA_P65, SIGMA_P80, SIGMA_P93, SIGMA_P99
)


# ─────────────────────────────────────────────────────────────────────────────
# Jump-diffusion weight (Merton component)
#
# In the production formula `total_variance_per_bar = cv + λ·j²`, λ scales
# the jump variance contribution. Calibrated jointly with σ cuts.
#
# Source: arms/src/bench/FeeFormulaV2.sol Constants.lambda
# ─────────────────────────────────────────────────────────────────────────────

LAMBDA_JUMP_WEIGHT: Final[float] = 1.097


# ─────────────────────────────────────────────────────────────────────────────
# Hysteresis — applied to mode down-transitions to prevent flapping
#
# Up-transitions are instant (safety: shocks priced immediately).
# Down-transitions only fire when σ has fallen below 0.9 × the boundary.
#
# This cuts mode-changes/day from 33.7 (naive) to 23.5 (-30%) while
# preserving 100% of HIGH/EXTREME shock-coverage.
#
# Source: research/cooldown_matrix.py grid search result
# ─────────────────────────────────────────────────────────────────────────────

HYSTERESIS_EPS_DOWN: Final[float] = 0.10  # 10% below boundary required


# ─────────────────────────────────────────────────────────────────────────────
# Regime modes — 6-mode ladder
# ─────────────────────────────────────────────────────────────────────────────

REGIME_NAMES: Final[tuple[str, ...]] = (
    "RESTING",   # 0  ~46% time-share
    "LOW",       # 1  ~16%
    "NORMAL",    # 2  ~16%
    "ELEVATED",  # 3  ~14%
    "HIGH",      # 4  ~7%
    "EXTREME",   # 5  ~1%
)

# Empirical time-share over the 730-day calibration window.
# Useful for capacity planning + sanity checks.
REGIME_TIME_SHARE_PCT: Final[dict[str, float]] = {
    "RESTING":  46.39,
    "LOW":      15.55,
    "NORMAL":   15.96,
    "ELEVATED": 14.27,
    "HIGH":      6.71,
    "EXTREME":   1.12,
}


# ─────────────────────────────────────────────────────────────────────────────
# Regime risk premium — added to base rate per mode
#
# Derived from RegimeCaps.sol fee schedule:
#   Hook fee:    0.9 / 5 / 20 / 60 / 120 / 250 bp
#   Loan premium: 0 / 5 / 15 / 30 / 60 / 200 bp  (scaled to 24h-loan horizons)
#
# The scaling rationale:
# - RESTING fee covers swap noise, not directional risk → 0 bp loan premium
# - Loan premium scales sub-linearly with hook fee because:
#     (1) loans have collateral cushion that swaps don't
#     (2) liquidation slippage is bounded by collateral, not fee notional
#
# Source: arms/src/bench/RegimeCaps.sol (FEE_R_*_PIPS constants)
# ─────────────────────────────────────────────────────────────────────────────

REGIME_PREMIUM_BPS: Final[dict[str, float]] = {
    "RESTING":    0.0,
    "LOW":        5.0,
    "NORMAL":    15.0,
    "ELEVATED":  30.0,
    "HIGH":      60.0,
    "EXTREME":  200.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Regime max LTV — hard caps protecting lenders during volatility shocks
#
# Even if mathematical max LTV (based on σ_T + lender's max_default_prob)
# is higher, we cap at these regime-specific values. Protects against
# jump-driven defaults that continuous variance models underweight.
# ─────────────────────────────────────────────────────────────────────────────

REGIME_MAX_LTV: Final[dict[str, float]] = {
    # Audit round-1 enforced 3% buffer below contract liquidation threshold
    # (95% contract threshold − 2% origination buffer = 93% absolute cap;
    # we set caps a further 1-2% below to give matching engine wiggle room).
    "RESTING":  0.92,   # was 0.98 — still way more efficient than Aave 80%
    "LOW":      0.90,   # was 0.96
    "NORMAL":   0.85,   # was 0.92
    "ELEVATED": 0.80,   # was 0.85 — matches Aave static cap
    "HIGH":     0.70,   # was 0.75 — 10% safer than Aave in stress
    "EXTREME":  0.55,   # was 0.60 — matching paused entirely in EXTREME anyway
}

# Regimes where matching is paused entirely (no quotes generated)
MATCHING_PAUSE_REGIMES: Final[frozenset[str]] = frozenset({"EXTREME"})


# ─────────────────────────────────────────────────────────────────────────────
# Rate aggregator source weights
#
# Total = 1.0. Higher weight = more influence on Agent-SOFR fair rate.
# Market-derived sources weighted highest; governance-set sources are
# reference-only; macro anchors keep rate near real-economy USD short rate.
#
# Sum: market 0.70 + reference 0.20 + macro 0.10 = 1.00
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_WEIGHTS: Final[dict[str, float]] = {
    # Market-derived (70%)
    "deribit_pcp_30d":       0.30,  # Deepest options market — hardest to manipulate
    "hl_funding_smoothed":   0.20,  # Largest perp venue — demand signal
    "aevo_pcp":              0.10,  # Cross-check on options markets
    "deribit_basis_3m":      0.10,  # Futures cost-of-carry sanity check

    # Reference-only (20%) — governance-set, so capped influence
    "aave_borrow_usdc":      0.10,
    "compound_borrow_usdc":  0.05,
    "aave_borrow_weth":      0.05,

    # Macro anchor (10%) — prevents detachment from real-economy USD rate
    "sofr_30d":              0.10,
}


# ─────────────────────────────────────────────────────────────────────────────
# ETH staking yield — convenience yield for ETH options PCP → USD rate conversion
#
# ETH options PCP gives (r_USD − q_ETH) where q_ETH is the convenience yield
# of holding ETH (primarily Lido stETH yield). To extract pure r_USD we add
# q_ETH back. Refreshed daily from Lido API.
# ─────────────────────────────────────────────────────────────────────────────

ETH_STAKING_YIELD_PCT_DEFAULT: Final[float] = 3.0  # %, slow-moving fallback


# ─────────────────────────────────────────────────────────────────────────────
# Loss-given-default — conservative assumption for collateral recovery
#
# Used in P_default × LGD × LTV variance_premium calc.
# 5% = assumes 95% recovery from liquidation slippage in normal conditions.
# Worst-case (cascading liquidations) could be higher; the regime_premium
# already pads for this.
# ─────────────────────────────────────────────────────────────────────────────

LGD_DEFAULT: Final[float] = 0.05


# ─────────────────────────────────────────────────────────────────────────────
# Default lender risk tolerance — used when intent omits max_default_prob
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MAX_DEFAULT_PROB: Final[float] = 0.001  # 0.1% probability tolerance


# ─────────────────────────────────────────────────────────────────────────────
# Asset addresses on Base mainnet — used by quote engine + matcher
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetMeta:
    """Metadata for a tradable asset on Base mainnet."""
    symbol: str
    address: str  # ERC-20 contract address on Base mainnet
    decimals: int
    is_stablecoin: bool


BASE_ASSETS: Final[dict[str, AssetMeta]] = {
    "USDC": AssetMeta(
        symbol="USDC",
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        decimals=6,
        is_stablecoin=True,
    ),
    "WETH": AssetMeta(
        symbol="WETH",
        address="0x4200000000000000000000000000000000000006",
        decimals=18,
        is_stablecoin=False,
    ),
    "EURC": AssetMeta(
        symbol="EURC",
        address="0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
        decimals=6,
        is_stablecoin=True,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Deployed contracts on Base mainnet (chain_id 8453)
# ─────────────────────────────────────────────────────────────────────────────

BASE_CHAIN_ID: Final[int] = 8453

# InterAgentRepo V1 — deployed 2026-05-21. MVP-no-liquidation demonstration.
INTERAGENT_REPO_V1_ADDRESS: Final[str] = "0xaea176DDa786c8B14802f92385749C7Cdf6C7400"

# InterAgentRepo V2 — deployed 2026-05-22. Chainlink liquidation.
# RETIRED 2026-05-22 via setOracleSigner(0x...dEaD) — tx 0x889a460824d949a119d37c53e14163db12998f640dd75b4a51e3c9e5809b37ba
# Per audit R2-#3. New originations on V2 always revert (no valid signer).
INTERAGENT_REPO_V2_ADDRESS: Final[str] = "0x2bfE0f1142B04049d867389Bf91A84e498ED11E4"

# InterAgentRepo V3 — deployed 2026-05-22. Audit round-1 patched.
# Deploy tx: 0x2ac8943ad54821ecdfe647da185cfe7e65c6812b512c54ddedbd7267ada186a7
# Superseded by V4 after audit round-2 found R2-#2 (pause-to-default DOS).
INTERAGENT_REPO_V3_ADDRESS: Final[str] = "0xFfca5d80c3413Bd5D17971550cCD615f57f22945"

# InterAgentRepo V4 — deployed 2026-05-22 via script/DeployV4.s.sol
# Deploy tx: 0xf7376511cbbba7a2da057bd046c1153e6566ef7d3d6462decdb8183b15b3af09
# R2-#2 fix: removed `whenNotPaused` from repay() — pause must block ENTRY
# (originate/liquidate/defaultLoan), not EXIT (repay). Industry standard:
# Aave V3, Compound III, Morpho Blue all keep repay always available.
# All V3 fixes (audit round-1) inherited verbatim.
INTERAGENT_REPO_V4_ADDRESS: Final[str] = "0x9d3b61d13a839968ffad94a0eedf73153c2fb31c"

# Active contract for new quotes — V4 is production
INTERAGENT_REPO_ADDRESS: Final[str] = INTERAGENT_REPO_V4_ADDRESS

# EIP-712 domain — must match the contract's _domainSeparatorV4()
EIP712_DOMAIN_NAME: Final[str] = "InterAgentRepo"
EIP712_DOMAIN_VERSION: Final[str] = "4"

# V3 audit-driven new constants
MIN_LTV_BUFFER_BPS: Final[int] = 200             # initial LTV must be < (95% - 2%) = 93%
MIN_DURATION_BUFFER_SECONDS: Final[int] = 60     # duration must be > grace + 60s = 120s
MAX_RATE_BPS: Final[int] = 100_000               # 1000% APR ceiling (sanity)

# Chainlink price feeds on Base mainnet — read into V2 contract for liquidation
CHAINLINK_ETH_USD_BASE: Final[str] = "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"
CHAINLINK_USDC_USD_BASE: Final[str] = "0x7e860098F58bBFC8648a4311b374B1D669a2bc6B"

# Liquidation parameters (mirrored from contract constants for off-chain monitoring)
LIQUIDATION_LTV_BPS: Final[int] = 9_500           # 95% — current LTV ≥ this → liquidatable
LIQUIDATOR_BOUNTY_BPS: Final[int] = 300            # 3% of collateral to liquidator
INSURANCE_FEE_BPS: Final[int] = 100                # 1% of collateral to insurance pool
LIQUIDATION_GRACE_SECONDS: Final[int] = 60         # anti-flash defense window
PRICE_STALENESS_LIMIT_SECONDS: Final[int] = 3600   # Chainlink heartbeat tolerance


# ─────────────────────────────────────────────────────────────────────────────
# x402 endpoint pricing — USDC per request, on Base mainnet
# ─────────────────────────────────────────────────────────────────────────────

PRICE_VRP_USDC: Final[float]          = 0.005   # /v1/asset/{eth,btc}/vrp
PRICE_RATE_QUERY_USDC: Final[float]   = 0.10    # /v1/rate/sofr/usd (Messari Enterprise tier)
PRICE_MAX_LTV_USDC: Final[float]      = 0.005   # /v1/risk/max-ltv
PRICE_LOAN_QUOTE_FLAT_USDC: Final[float] = 0.05  # /v1/quote — flat floor
PRICE_LOAN_QUOTE_BPS: Final[int]      = 5       # /v1/quote — 5 bps of principal (max of flat or %)

# Cache TTL — how long a quote is valid before re-computation
CACHE_TTL_SEC: Final[int] = 60


# ─────────────────────────────────────────────────────────────────────────────
# Bar size — used for variance scaling
# ─────────────────────────────────────────────────────────────────────────────

BAR_SECONDS: Final[int] = 300  # 5-minute bars match ARMSHookV3 calibration
BARS_PER_HOUR: Final[int] = 12
BARS_PER_DAY: Final[int] = 288
BARS_PER_YEAR: Final[int] = 288 * 365


# ─────────────────────────────────────────────────────────────────────────────
# Sanity floor / ceiling — protect against pathological rate outputs
#
# If the computed rate falls outside this range, something is broken and
# we should not publish it. Triggers a fall-through to last-known-good.
# ─────────────────────────────────────────────────────────────────────────────

RATE_FLOOR_PCT: Final[float] = 0.0   # Negative rates not yet supported
RATE_CEILING_PCT: Final[float] = 50.0  # Cap at 50% — anything higher = broken signal


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: derived constants for the 6-mode ladder
# ─────────────────────────────────────────────────────────────────────────────

NUM_REGIMES: Final[int] = len(REGIME_NAMES)


def regime_name(idx: int) -> str:
    """Map regime index (0..5) → name. Defensive: clips to valid range."""
    if idx < 0:
        idx = 0
    elif idx >= NUM_REGIMES:
        idx = NUM_REGIMES - 1
    return REGIME_NAMES[idx]


def regime_index(name: str) -> int:
    """Map regime name → index. Raises KeyError on unknown name."""
    try:
        return REGIME_NAMES.index(name)
    except ValueError:
        raise KeyError(f"Unknown regime: {name!r}")


__all__ = [
    # Methodology
    "METHODOLOGY_VERSION", "CALIBRATION_SOURCE", "CALIBRATION_DATA",
    "METHODOLOGY_IPFS_HASH",
    # Sigma thresholds
    "SIGMA_P50", "SIGMA_P65", "SIGMA_P80", "SIGMA_P93", "SIGMA_P99", "SIGMA_CUTS",
    # Jump weight
    "LAMBDA_JUMP_WEIGHT",
    # Hysteresis
    "HYSTERESIS_EPS_DOWN",
    # Regime tables
    "REGIME_NAMES", "REGIME_TIME_SHARE_PCT", "REGIME_PREMIUM_BPS",
    "REGIME_MAX_LTV", "MATCHING_PAUSE_REGIMES", "NUM_REGIMES",
    "regime_name", "regime_index",
    # Source weights
    "SOURCE_WEIGHTS",
    # Risk params
    "ETH_STAKING_YIELD_PCT_DEFAULT", "LGD_DEFAULT", "DEFAULT_MAX_DEFAULT_PROB",
    # Asset metadata
    "AssetMeta", "BASE_ASSETS",
    # Pricing
    "PRICE_RATE_QUERY_USDC", "PRICE_MAX_LTV_USDC", "PRICE_LOAN_QUOTE_USDC",
    "CACHE_TTL_SEC",
    # Time
    "BAR_SECONDS", "BARS_PER_HOUR", "BARS_PER_DAY", "BARS_PER_YEAR",
    # Sanity bounds
    "RATE_FLOOR_PCT", "RATE_CEILING_PCT",
]
