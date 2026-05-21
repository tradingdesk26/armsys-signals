"""
Agent-SOFR oracle — agent-native short-term rate benchmark.

Exposes:
    - calibration: production-tested constants from ARMSHookV3
    - regime_classifier: 6-mode classifier with hysteresis
    - variance_engine: live cv + j² computation
    - rate_aggregator: multi-source weighted median
    - max_ltv: dynamic LTV based on variance + regime
    - agent_sofr: main entry point
"""

from oracle import calibration

__version__ = "0.1.0"
