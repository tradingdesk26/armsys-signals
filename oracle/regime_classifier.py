"""
6-mode regime classifier with 10% down-hysteresis.

Python port of arms/src/bench/FeeFormulaV2.sol:classifyModeHyst.
Bit-equivalent (within float vs uint256 precision) to the on-chain logic.

State model:
    Up-transitions are instant — when σ rises above a boundary, mode steps
    up immediately. This prioritizes safety: shocks are priced without lag.

    Down-transitions require σ to fall 10% below the boundary that would
    otherwise trigger the step. This prevents flapping at percentile cuts.

    The classifier maintains `last_mode` across calls — the same σ value
    can produce different modes depending on the prior mode (hysteresis path).

Cold-start behavior:
    First call (last_mode=None) uses naive classification (no hysteresis),
    so the classifier starts in the mode appropriate for current σ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from oracle.calibration import (
    SIGMA_CUTS, REGIME_NAMES, NUM_REGIMES, HYSTERESIS_EPS_DOWN,
    regime_name as _regime_name,
)


# Precomputed boundaries for fast comparison
# down_boundary[m] = the boundary you must fall 10% below to step DOWN from mode m
# (this is the SAME boundary as the up-cut from mode m-1)
_DOWN_HYSTERESIS_MULTIPLIER: Final[float] = 1.0 - HYSTERESIS_EPS_DOWN  # 0.9


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of a single classification call."""
    mode_index: int             # 0..5
    mode_name: str              # human-readable (matches REGIME_NAMES)
    sigma: float                # input σ (fractional, e.g. 0.002 = 20 bp)
    sigma_bp: float             # input σ in basis points
    prior_mode_index: Optional[int]    # what mode we came from (None on cold-start)
    transition: str             # "up" | "down" | "hold" | "init"

    def __repr__(self) -> str:
        prior = self.prior_mode_index if self.prior_mode_index is not None else "INIT"
        return (
            f"ClassificationResult(mode={self.mode_name}[{self.mode_index}], "
            f"σ={self.sigma_bp:.2f}bp, prior={prior}, transition={self.transition})"
        )


def _naive_classify(sigma: float) -> int:
    """
    Naive classifier — no hysteresis. Used for cold-start and as a building block.
    Returns mode index where sigma falls in [cut[i-1], cut[i]).
    """
    if sigma >= SIGMA_CUTS[4]:    # > p99
        return 5  # EXTREME
    if sigma >= SIGMA_CUTS[3]:    # > p93
        return 4  # HIGH
    if sigma >= SIGMA_CUTS[2]:    # > p80
        return 3  # ELEVATED
    if sigma >= SIGMA_CUTS[1]:    # > p65
        return 2  # NORMAL
    if sigma >= SIGMA_CUTS[0]:    # > p50
        return 1  # LOW
    return 0  # RESTING


def classify_with_hysteresis(sigma: float, last_mode: Optional[int]) -> int:
    """
    Pure function: classify σ given prior mode.

    Mirrors the Solidity classifyModeHyst byte-for-byte (within float precision).

    Args:
        sigma: current 5-min σ (fractional, e.g. 0.0014 = 14 bp)
        last_mode: prior mode index (0..5), or None for cold-start

    Returns:
        New mode index ∈ {0..5}.
    """
    # Cold-start: no prior mode → use naive classification
    if last_mode is None:
        return _naive_classify(sigma)

    # Defensive clip on prior mode
    m = max(0, min(last_mode, NUM_REGIMES - 1))

    # Up-transitions — instant. Walk up while σ exceeds the next boundary.
    # Boundary i ∈ 0..4 separates mode i from mode i+1.
    while m < NUM_REGIMES - 1:
        up_cut = SIGMA_CUTS[m]
        if sigma >= up_cut:
            m += 1
        else:
            break

    # Down-transitions — only with 10% hysteresis. Walk down while σ has
    # fallen below 0.9 × the boundary that separates m-1 from m.
    while m > 0:
        down_cut_boundary = SIGMA_CUTS[m - 1]
        if sigma < down_cut_boundary * _DOWN_HYSTERESIS_MULTIPLIER:
            m -= 1
        else:
            break

    return m


class RegimeClassifier:
    """
    Stateful classifier — keeps last_mode across calls for hysteresis.

    Use this for live operation. The pure `classify_with_hysteresis` is for
    tests and replay scenarios where state is supplied externally.
    """

    def __init__(self, initial_sigma: Optional[float] = None):
        """
        Args:
            initial_sigma: if provided, bootstrap last_mode from this value
                using naive classification. Otherwise, last_mode = None
                until first call.
        """
        if initial_sigma is not None:
            self._last_mode: Optional[int] = _naive_classify(initial_sigma)
        else:
            self._last_mode = None

    def classify(self, sigma: float) -> ClassificationResult:
        """
        Classify current σ with hysteresis applied to last_mode.

        Updates internal state for subsequent calls.

        Args:
            sigma: current 5-min σ (fractional, e.g. 0.002 = 20 bp)

        Returns:
            ClassificationResult with mode + transition metadata.
        """
        prior = self._last_mode
        new_mode = classify_with_hysteresis(sigma, prior)

        # Determine transition type
        if prior is None:
            transition = "init"
        elif new_mode > prior:
            transition = "up"
        elif new_mode < prior:
            transition = "down"
        else:
            transition = "hold"

        self._last_mode = new_mode

        return ClassificationResult(
            mode_index=new_mode,
            mode_name=_regime_name(new_mode),
            sigma=sigma,
            sigma_bp=sigma * 10_000,
            prior_mode_index=prior,
            transition=transition,
        )

    @property
    def current_mode(self) -> Optional[int]:
        """Last classified mode, or None if never called."""
        return self._last_mode

    def reset(self, initial_sigma: Optional[float] = None) -> None:
        """Reset state — useful for testing or after long downtime."""
        if initial_sigma is not None:
            self._last_mode = _naive_classify(initial_sigma)
        else:
            self._last_mode = None


__all__ = [
    "ClassificationResult",
    "RegimeClassifier",
    "classify_with_hysteresis",
]
