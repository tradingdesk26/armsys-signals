"""
Two-tier x402 facilitator with transparent failover.

The Coinbase CDP facilitator is the primary paid-x402 settlement path —
brand-trusted, well-known, and Coinbase pays gas. As a resilience layer we
keep a self-hosted facilitator (`own_facilitator.py`) running on the same VM
as a secondary; if the primary times out, rate-limits, or returns any
exception, the secondary picks up verify+settle seamlessly.

Behaviour:
  - `verify()` and `settle()`: try primary; on any raised exception, retry
    on the secondary and log the failover event. If secondary also fails,
    raise the secondary error.
  - A 200 OK with `is_valid=False` is treated as a legitimate rejection
    (the signature really is bad / the authorization expired / etc.) and
    is NOT retried — the secondary would reject for the same reason.
  - `get_supported()` falls back the same way.

External clients (paying agents) see no behavioural difference. The 402
challenge still advertises the primary facilitator URL; the failover is
purely server-side resilience.
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("facilitator_failover")


class FallbackFacilitatorClient:
    """Async FacilitatorClient that retries on a secondary on any primary error.

    Mirrors the x402 SDK's ``FacilitatorClient`` Protocol:
      - ``async verify(payload, requirements) -> VerifyResponse``
      - ``async settle(payload, requirements) -> SettleResponse``
      - ``get_supported() -> SupportedResponse``

    plus the convenience attributes the SDK exposes on its concrete clients
    (``url``, ``identifier``) so this wrapper is a drop-in replacement.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        if primary is None or fallback is None:
            raise ValueError("FallbackFacilitatorClient needs both primary and fallback")
        self.primary = primary
        self.fallback = fallback

    # ─── identity surfaced to the x402 server ─────────────────────────────

    @property
    def url(self) -> str:
        return getattr(self.primary, "url", "")

    @property
    def identifier(self) -> str:
        prim = getattr(self.primary, "identifier", None) or getattr(self.primary, "url", "primary")
        return f"{prim} (+ fallback)"

    # ─── verify ───────────────────────────────────────────────────────────

    async def verify(self, payload, requirements):
        try:
            return await self.primary.verify(payload, requirements)
        except Exception as e:
            log.warning("primary verify failed (%s: %s) — failing over",
                        type(e).__name__, str(e)[:200])
            try:
                result = await self.fallback.verify(payload, requirements)
                log.info("fallback verify succeeded")
                return result
            except Exception as fe:
                log.error("fallback verify ALSO failed (%s: %s)",
                          type(fe).__name__, str(fe)[:200])
                raise

    # ─── settle ───────────────────────────────────────────────────────────

    async def settle(self, payload, requirements):
        try:
            return await self.primary.settle(payload, requirements)
        except Exception as e:
            log.warning("primary settle failed (%s: %s) — failing over",
                        type(e).__name__, str(e)[:200])
            try:
                result = await self.fallback.settle(payload, requirements)
                log.info("fallback settle succeeded")
                return result
            except Exception as fe:
                log.error("fallback settle ALSO failed (%s: %s)",
                          type(fe).__name__, str(fe)[:200])
                raise

    # ─── get_supported (sync per SDK protocol) ────────────────────────────

    def get_supported(self):
        try:
            return self.primary.get_supported()
        except Exception as e:
            log.warning("primary get_supported failed (%s: %s) — using fallback",
                        type(e).__name__, str(e)[:200])
            return self.fallback.get_supported()
