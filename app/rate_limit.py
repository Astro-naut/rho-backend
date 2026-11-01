"""Simple in-memory per-IP token-bucket rate limiter."""

from __future__ import annotations

import time
from threading import Lock
from typing import Dict, Tuple


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._tokens: Dict[str, Tuple[float, float]] = {}
        self._lock = Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._tokens.get(key, (self.capacity, now))
            elapsed = max(0.0, now - last)
            tokens = min(self.capacity, tokens + elapsed * self.refill_per_sec)
            if tokens < cost:
                self._tokens[key] = (tokens, now)
                return False
            self._tokens[key] = (tokens - cost, now)
            return True
