"""
Request counter for ARMS Signals — persisted to JSON, atomic-ish.

Keys:
  - {endpoint}.402  — unpaid request (preview / probe)
  - {endpoint}.200  — paid call (successful verify+settle)
  - {endpoint}.other — anything else (404, 500, etc.)

Persists to /opt/arms-signals/stats.json every flush.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

STATS_FILE = Path(os.getenv("STATS_FILE", "/opt/arms-signals/stats.json"))


class StatsCounter:
    def __init__(self, persist_path: Path = STATS_FILE,
                 flush_interval_sec: int = 10):
        self.persist_path = persist_path
        self.flush_interval = flush_interval_sec
        self._counts: dict[str, int] = defaultdict(int)
        self._started: int = int(time.time())
        self._lock = threading.Lock()
        self._dirty: bool = False
        self._load()

    def _load(self) -> None:
        if self.persist_path.exists():
            try:
                with open(self.persist_path) as f:
                    saved = json.load(f)
                self._counts.update(saved.get("counts", {}))
                # Preserve original "started" timestamp across restarts
                self._started = saved.get("started", self._started)
            except Exception:
                pass

    def _flush_unlocked(self) -> None:
        tmp = self.persist_path.with_suffix(".json.tmp")
        data = {
            "started":   self._started,
            "last_save": int(time.time()),
            "counts":    dict(self._counts),
        }
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.replace(self.persist_path)
            self._dirty = False
        except Exception:
            pass

    def incr(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counts[key] += n
            self._dirty = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._dirty:
                self._flush_unlocked()
            return {
                "started":   self._started,
                "uptime_sec": int(time.time() - self._started),
                "counts":    dict(self._counts),
            }
