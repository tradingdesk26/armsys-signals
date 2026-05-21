"""
Intent book — SQLite-backed order book for the Inter-Agent Clearinghouse.

Stores open lender/borrower intents + matched-quote records. The matcher
queries this to find compatible pairs.

Schema is simple. No partial fills in MVP — an intent is either open,
matched, or expired/cancelled.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Optional


DB_PATH = Path("/opt/arms-signals/intent_book.sqlite")


class IntentSide(str, Enum):
    LEND = "lend"
    BORROW = "borrow"


class IntentStatus(str, Enum):
    OPEN = "open"
    MATCHED = "matched"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class LenderIntent:
    intent_id: str
    wallet: str
    asset: str               # symbol (USDC, EURC, ETH)
    amount: float            # human units
    max_duration_sec: int
    min_rate_bps: int
    max_default_prob: float
    expires_at: int          # unix seconds — when this intent itself expires
    status: str
    matched_to: Optional[str] = None
    created_at: int = 0


@dataclass
class BorrowerIntent:
    intent_id: str
    wallet: str
    principal_asset: str
    principal_amount: float
    collateral_asset: str
    collateral_amount_max: float
    duration_sec: int
    max_rate_bps: int
    expires_at: int
    status: str
    matched_to: Optional[str] = None
    created_at: int = 0


@dataclass
class Match:
    match_id: str
    lender_intent_id: str
    borrower_intent_id: str
    quote_payload: str       # JSON-serialized SignedQuote
    created_at: int


# ─────────────────────────────────────────────────────────────────────────────
# Storage layer
# ─────────────────────────────────────────────────────────────────────────────

class IntentBook:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS lender_intents (
                intent_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                asset TEXT NOT NULL,
                amount REAL NOT NULL,
                max_duration_sec INTEGER NOT NULL,
                min_rate_bps INTEGER NOT NULL,
                max_default_prob REAL NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                matched_to TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lender_status ON lender_intents(status);
            CREATE INDEX IF NOT EXISTS idx_lender_asset ON lender_intents(asset);

            CREATE TABLE IF NOT EXISTS borrower_intents (
                intent_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                principal_asset TEXT NOT NULL,
                principal_amount REAL NOT NULL,
                collateral_asset TEXT NOT NULL,
                collateral_amount_max REAL NOT NULL,
                duration_sec INTEGER NOT NULL,
                max_rate_bps INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                matched_to TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_borrower_status ON borrower_intents(status);
            CREATE INDEX IF NOT EXISTS idx_borrower_asset ON borrower_intents(principal_asset);

            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                lender_intent_id TEXT NOT NULL,
                borrower_intent_id TEXT NOT NULL,
                quote_payload TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_match_lender ON matches(lender_intent_id);
            CREATE INDEX IF NOT EXISTS idx_match_borrower ON matches(borrower_intent_id);
        """)
        self._conn.commit()

    # ─── Intent submission ──────────────────────────────────────────────────

    def add_lender(self, intent: dict) -> LenderIntent:
        intent_id = "lend_" + secrets.token_hex(8)
        now = int(time.time())
        row = LenderIntent(
            intent_id=intent_id,
            wallet=intent["wallet"],
            asset=intent["asset"].upper(),
            amount=float(intent["amount"]),
            max_duration_sec=int(intent["max_duration_sec"]),
            min_rate_bps=int(intent["min_rate_bps"]),
            max_default_prob=float(intent.get("max_default_prob", 0.001)),
            expires_at=int(intent.get("expires_at", now + 1800)),
            status=IntentStatus.OPEN.value,
            created_at=now,
        )
        self._conn.execute("""
            INSERT INTO lender_intents
              (intent_id, wallet, asset, amount, max_duration_sec, min_rate_bps,
               max_default_prob, expires_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.intent_id, row.wallet, row.asset, row.amount,
            row.max_duration_sec, row.min_rate_bps, row.max_default_prob,
            row.expires_at, row.status, row.created_at,
        ))
        self._conn.commit()
        return row

    def add_borrower(self, intent: dict) -> BorrowerIntent:
        intent_id = "bor_" + secrets.token_hex(8)
        now = int(time.time())
        row = BorrowerIntent(
            intent_id=intent_id,
            wallet=intent["wallet"],
            principal_asset=intent["principal_asset"].upper(),
            principal_amount=float(intent["principal_amount"]),
            collateral_asset=intent["collateral_asset"].upper(),
            collateral_amount_max=float(intent["collateral_amount_max"]),
            duration_sec=int(intent["duration_sec"]),
            max_rate_bps=int(intent["max_rate_bps"]),
            expires_at=int(intent.get("expires_at", now + 1800)),
            status=IntentStatus.OPEN.value,
            created_at=now,
        )
        self._conn.execute("""
            INSERT INTO borrower_intents
              (intent_id, wallet, principal_asset, principal_amount,
               collateral_asset, collateral_amount_max, duration_sec,
               max_rate_bps, expires_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.intent_id, row.wallet, row.principal_asset, row.principal_amount,
            row.collateral_asset, row.collateral_amount_max, row.duration_sec,
            row.max_rate_bps, row.expires_at, row.status, row.created_at,
        ))
        self._conn.commit()
        return row

    # ─── Queries ────────────────────────────────────────────────────────────

    def open_lenders(self, asset: Optional[str] = None) -> list[LenderIntent]:
        now = int(time.time())
        q = """
            SELECT * FROM lender_intents
            WHERE status = 'open' AND expires_at > ?
        """
        params: list = [now]
        if asset:
            q += " AND asset = ?"
            params.append(asset.upper())
        q += " ORDER BY min_rate_bps ASC"
        rows = self._conn.execute(q, params).fetchall()
        return [LenderIntent(**dict(r)) for r in rows]

    def open_borrowers(self, principal_asset: Optional[str] = None) -> list[BorrowerIntent]:
        now = int(time.time())
        q = """
            SELECT * FROM borrower_intents
            WHERE status = 'open' AND expires_at > ?
        """
        params: list = [now]
        if principal_asset:
            q += " AND principal_asset = ?"
            params.append(principal_asset.upper())
        q += " ORDER BY max_rate_bps DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [BorrowerIntent(**dict(r)) for r in rows]

    def recent_matches(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute("""
            SELECT * FROM matches ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ─── State updates ──────────────────────────────────────────────────────

    def mark_lender_matched(self, intent_id: str, match_id: str) -> None:
        self._conn.execute("""
            UPDATE lender_intents SET status = 'matched', matched_to = ?
            WHERE intent_id = ? AND status = 'open'
        """, (match_id, intent_id))
        self._conn.commit()

    def mark_borrower_matched(self, intent_id: str, match_id: str) -> None:
        self._conn.execute("""
            UPDATE borrower_intents SET status = 'matched', matched_to = ?
            WHERE intent_id = ? AND status = 'open'
        """, (match_id, intent_id))
        self._conn.commit()

    def record_match(self, lender_id: str, borrower_id: str, quote_payload: dict) -> Match:
        match_id = "match_" + secrets.token_hex(8)
        now = int(time.time())
        self._conn.execute("""
            INSERT INTO matches (match_id, lender_intent_id, borrower_intent_id, quote_payload, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (match_id, lender_id, borrower_id, json.dumps(quote_payload), now))
        self.mark_lender_matched(lender_id, match_id)
        self.mark_borrower_matched(borrower_id, match_id)
        return Match(
            match_id=match_id,
            lender_intent_id=lender_id,
            borrower_intent_id=borrower_id,
            quote_payload=json.dumps(quote_payload),
            created_at=now,
        )

    def cancel(self, intent_id: str, side: IntentSide) -> bool:
        table = "lender_intents" if side == IntentSide.LEND else "borrower_intents"
        cur = self._conn.execute(f"""
            UPDATE {table} SET status = 'cancelled'
            WHERE intent_id = ? AND status = 'open'
        """, (intent_id,))
        self._conn.commit()
        return cur.rowcount > 0


__all__ = [
    "IntentBook", "LenderIntent", "BorrowerIntent", "Match",
    "IntentSide", "IntentStatus",
]
