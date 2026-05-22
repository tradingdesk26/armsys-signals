"""
Matcher — pairs open lender intents with open borrower intents,
generates signed quotes, records matches.

Algorithm:
    1. Pull open lenders + borrowers from intent book
    2. Filter pairs by asset + duration compatibility + rate compatibility
    3. For each compatible pair, ask quote engine for a quote in
       compute_collateral mode (lender's min_rate is the floor)
    4. Check that quoted collateral ≤ borrower's max collateral
    5. Take first match (sorted by clearing rate ascending — best deal first)
    6. Record + mark intents as matched
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Optional

from matcher.intent_book import (
    IntentBook, LenderIntent, BorrowerIntent, Match,
)
from matcher.quote_engine import QuoteEngine, SignedQuote
from oracle.calibration import REGIME_MAX_LTV


# ─────────────────────────────────────────────────────────────────────────────
# Webhook firing — best-effort, async (fire-and-forget thread per webhook)
# ─────────────────────────────────────────────────────────────────────────────

WEBHOOK_TIMEOUT_SEC: float = 5.0
WEBHOOK_USER_AGENT: str = "regimeshift-clearinghouse-webhook/1.0"


def _fire_webhook_async(url: str, payload: dict) -> None:
    """
    POST payload to url with a tight timeout, fire-and-forget.
    Does not retry on failure — webhook consumer is responsible for idempotency.
    Best-effort: any exception swallowed (will be visible only in container logs).
    """
    if not url:
        return

    def _worker() -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": WEBHOOK_USER_AGENT,
                    "X-RegimeShift-Event": payload.get("event", "match_found"),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SEC) as resp:
                resp.read()  # drain
        except Exception:
            # Log silently — in prod we'd push to monitoring; for MVP, best-effort.
            pass

    threading.Thread(target=_worker, daemon=True).start()


class Matcher:
    def __init__(self, book: IntentBook, engine: QuoteEngine):
        self.book = book
        self.engine = engine

    def find_match(
        self,
        collateral_price_usd: float = 2080.0,  # caller can pass live price
    ) -> Optional[Match]:
        """
        Run one matching cycle. Returns the first successful match, or None.
        """
        # Pull open intents from both sides
        lenders = self.book.open_lenders()
        borrowers = self.book.open_borrowers()

        if not lenders or not borrowers:
            return None

        # Find compatible pairs
        for borrower in borrowers:
            for lender in lenders:
                # Asset compat
                if lender.asset != borrower.principal_asset:
                    continue
                # Amount compat (lender must have enough)
                if lender.amount < borrower.principal_amount:
                    continue
                # Duration compat (lender's max ≥ borrower's request)
                if lender.max_duration_sec < borrower.duration_sec:
                    continue
                # Rate compat (lender's min ≤ borrower's max — clearable spread)
                if lender.min_rate_bps > borrower.max_rate_bps:
                    continue

                # Try to build a quote at lender's min_rate (cheapest for borrower)
                # using compute_collateral mode — this tells us collateral needed
                try:
                    quote = self.engine.compute_collateral(
                        principal_amount_usd=borrower.principal_amount,
                        target_rate_bps=lender.min_rate_bps,
                        duration_sec=borrower.duration_sec,
                        borrower=borrower.wallet,
                        lender=lender.wallet,
                        principal_asset=borrower.principal_asset,
                        collateral_asset=borrower.collateral_asset,
                        collateral_price_usd=collateral_price_usd,
                    )
                except ValueError as e:
                    # Rate too low to clear premium → try next pair
                    # If we wanted to be smarter, we'd retry at the borrower's max_rate
                    # (giving them less collateral relief), but MVP: skip
                    continue

                # Convert quoted collateral to human units for comparison
                from oracle.calibration import BASE_ASSETS
                c_meta = BASE_ASSETS[borrower.collateral_asset]
                quoted_collateral_native = quote.collateral_amount / (10 ** c_meta.decimals)

                if quoted_collateral_native > borrower.collateral_amount_max:
                    # Borrower can't post that much — try the next pair
                    continue

                # Match!
                match = self.book.record_match(
                    lender_id=lender.intent_id,
                    borrower_id=borrower.intent_id,
                    quote_payload=quote.to_dict(),
                )

                # Fire webhooks (best-effort) — both sides get notified if they
                # provided a webhook_url at intent submission.
                webhook_payload = {
                    "event": "match_found",
                    "match_id": match.match_id,
                    "lender_intent_id": match.lender_intent_id,
                    "borrower_intent_id": match.borrower_intent_id,
                    "quote": quote.to_dict(),
                    "created_at": match.created_at,
                }
                if lender.webhook_url:
                    _fire_webhook_async(
                        lender.webhook_url,
                        {**webhook_payload, "your_role": "lender", "your_intent_id": lender.intent_id},
                    )
                if borrower.webhook_url:
                    _fire_webhook_async(
                        borrower.webhook_url,
                        {**webhook_payload, "your_role": "borrower", "your_intent_id": borrower.intent_id},
                    )

                return match

        return None

    def run_until_no_matches(self, max_iterations: int = 50) -> list[Match]:
        """
        Keep matching until no more matches found (or hit safety limit).
        Returns list of matches found.
        """
        out: list[Match] = []
        for _ in range(max_iterations):
            m = self.find_match()
            if m is None:
                break
            out.append(m)
        return out


__all__ = ["Matcher"]
