"""
Maximum-safe loan-to-value calculator.

Given a loan configuration (asset, duration, regime, lender risk tolerance),
returns the highest LTV that keeps default probability below the lender's
acceptable threshold.

Uses two-stage safety:
    1. Mathematical max from σ-over-horizon + Black-Cox first-passage
    2. Regime hard cap (additional protection against jump risk that the
       continuous-variance model underweights)

The smaller of the two is binding.

Standalone API endpoint (paid $0.001 via x402):
    GET /v1/risk/max-ltv?asset=ETH&duration_sec=3600&max_default_prob=0.001

Used by:
    - InterAgentRepo matcher (validate borrower collateral sufficient)
    - Quote engine (compute_collateral mode)
    - Any external agent doing their own loan logic
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

from scipy.stats import norm

from oracle.calibration import (
    REGIME_MAX_LTV, MATCHING_PAUSE_REGIMES,
    DEFAULT_MAX_DEFAULT_PROB,
    LGD_DEFAULT,
)
from oracle.variance_engine import VarianceSnapshot


@dataclass(frozen=True)
class MaxLTVResult:
    """
    Output of max_safe_ltv() — both numerical answer and decomposition.
    """
    # Final answer
    max_ltv: float                  # The actual maximum LTV (0.0 - 1.0)

    # Decomposition — which constraint is binding
    math_max_ltv: float             # From σ_T + max_default_prob
    regime_cap_ltv: float           # Hard cap from regime
    binding_constraint: Literal["math", "regime_cap", "matching_paused"]

    # Inputs (for verification)
    asset: str
    duration_sec: int
    regime: str
    max_default_prob: float

    # Intermediate
    sigma_T: float                  # σ over the loan horizon (fractional)
    matching_paused: bool           # True if regime is in MATCHING_PAUSE_REGIMES

    def to_dict(self) -> dict:
        """JSON-friendly representation for API response."""
        return {
            "max_ltv": self.max_ltv,
            "math_max_ltv": self.math_max_ltv,
            "regime_cap_ltv": self.regime_cap_ltv,
            "binding_constraint": self.binding_constraint,
            "asset": self.asset,
            "duration_sec": self.duration_sec,
            "regime": self.regime,
            "max_default_prob": self.max_default_prob,
            "sigma_T": self.sigma_T,
            "matching_paused": self.matching_paused,
        }


def compute_math_max_ltv(
    sigma_T: float,
    max_default_prob: float = DEFAULT_MAX_DEFAULT_PROB,
) -> float:
    """
    Mathematical max LTV based on Black-Scholes-style first-passage probability.

    Model:
        Let LTV = principal / collateral_value
        Liquidation happens if collateral drops by (1 - LTV) fraction.
        For lognormal price diffusion over horizon T:
            P(price drops > (1-LTV)) ≈ Φ(-ln(1/LTV) / σ_T)

    Setting P_default ≤ max_default_prob and solving for LTV:
        ln(1/LTV) ≥ |Φ^(-1)(max_default_prob)| × σ_T
        LTV ≤ exp(-z × σ_T)   where z = |Φ^(-1)(max_default_prob)|

    Note: This is a SIMPLIFIED model — it underweights jump risk.
    The regime cap layer compensates for that.

    Args:
        sigma_T: σ over the loan horizon (fractional, e.g. 0.005 = 0.5%)
        max_default_prob: lender's acceptable default probability (e.g. 0.001 = 0.1%)

    Returns:
        Mathematical max LTV in [0.0, 1.0].
    """
    if sigma_T <= 0:
        return 1.0  # zero variance → any LTV is safe (degenerate case)
    if max_default_prob <= 0 or max_default_prob >= 1:
        raise ValueError(f"max_default_prob must be in (0, 1), got {max_default_prob}")

    # z-score for one-sided tail: P(Z < -z) = max_default_prob
    z = abs(norm.ppf(max_default_prob))

    # Solve: LTV = exp(-z × σ_T)
    ltv = math.exp(-z * sigma_T)

    return max(0.0, min(1.0, ltv))


def max_safe_ltv(
    variance: VarianceSnapshot,
    duration_sec: int,
    regime: str,
    max_default_prob: float = DEFAULT_MAX_DEFAULT_PROB,
    asset: str = "ETH",
) -> MaxLTVResult:
    """
    Compute the max safe LTV for a loan, taking the min of:
        - Mathematical max (from variance + default tolerance)
        - Regime hard cap (additional jump-risk protection)

    Args:
        variance: VarianceSnapshot from variance_engine (current cv + j²)
        duration_sec: loan duration in seconds
        regime: current regime name (e.g. "NORMAL")
        max_default_prob: lender's acceptable default probability
        asset: collateral asset symbol (for the response, doesn't affect math)

    Returns:
        MaxLTVResult with the final max_ltv plus decomposition.
    """
    if duration_sec <= 0:
        raise ValueError(f"duration_sec must be positive, got {duration_sec}")
    if regime not in REGIME_MAX_LTV:
        raise ValueError(f"Unknown regime: {regime!r}")

    # σ scaled to the loan horizon (BS-iid assumption)
    sigma_T = variance.sigma_over_horizon(duration_sec)

    # Check if matching paused entirely
    matching_paused = regime in MATCHING_PAUSE_REGIMES

    # Layer 1: Math max
    math_max = compute_math_max_ltv(sigma_T, max_default_prob)

    # Layer 2: Regime cap
    regime_cap = REGIME_MAX_LTV[regime]

    # Final = min of both (most conservative wins)
    final_ltv = min(math_max, regime_cap)

    # Determine binding constraint
    if matching_paused:
        binding: Literal["math", "regime_cap", "matching_paused"] = "matching_paused"
        final_ltv = 0.0  # explicitly zero out when matching paused
    elif regime_cap < math_max:
        binding = "regime_cap"
    else:
        binding = "math"

    return MaxLTVResult(
        max_ltv=final_ltv,
        math_max_ltv=math_max,
        regime_cap_ltv=regime_cap,
        binding_constraint=binding,
        asset=asset,
        duration_sec=duration_sec,
        regime=regime,
        max_default_prob=max_default_prob,
        sigma_T=sigma_T,
        matching_paused=matching_paused,
    )


def required_collateral(
    principal_amount: float,
    principal_price_usd: float,
    collateral_price_usd: float,
    max_ltv: float,
) -> float:
    """
    Convenience: given principal + max LTV, compute required collateral.

    collateral_value = principal_value / max_ltv
    collateral_amount = collateral_value / collateral_price

    Args:
        principal_amount: how much principal is being borrowed
        principal_price_usd: USD price of principal asset (1.0 for USDC)
        collateral_price_usd: USD price of collateral asset (e.g. ETH spot)
        max_ltv: result from max_safe_ltv() (in [0.0, 1.0])

    Returns:
        Required collateral amount in native units.
    """
    if max_ltv <= 0:
        raise ValueError("max_ltv must be positive (got matching_paused state)")
    if max_ltv > 1:
        raise ValueError(f"max_ltv > 1 doesn't make sense, got {max_ltv}")

    principal_value_usd = principal_amount * principal_price_usd
    collateral_value_usd = principal_value_usd / max_ltv
    return collateral_value_usd / collateral_price_usd


__all__ = [
    "MaxLTVResult",
    "max_safe_ltv",
    "compute_math_max_ltv",
    "required_collateral",
]
