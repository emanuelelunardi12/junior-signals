"""Request-level protection: rate limits (and, from Part 5, headers and origin checks).

The app runs as ONE process (see architecturedoc.md), so an in-memory limiter is enough and needs no Redis.
If we ever run several workers, each gets its own buckets; limits then apply per worker.
"""
import threading
import time
from collections import defaultdict

# (requests, per seconds). Generous for a person, tight for a script.
LIMITS = {
    "events": (60, 60),        # behaviour batches: a page sends one every ~10 s
    "feedback": (30, 60),      # Useful / Not useful taps
    "comment": (5, 600),       # optional free-text comments
    "search": (60, 60),        # ticker search
    "watchlist": (30, 60),
    "login": (20, 60),
    "settings": (20, 60),
    "insights": (3, 600),      # each run is a paid AI call
}


class RateLimiter:
    """Sliding-window counter per (bucket, key)."""

    def __init__(self, limits: dict[str, tuple[int, int]]):
        self.limits = limits
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, bucket: str, key: str) -> bool:
        limit, window = self.limits[bucket]
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits[(bucket, key)] if now - t < window]
            if len(hits) >= limit:
                self._hits[(bucket, key)] = hits
                return False
            hits.append(now)
            self._hits[(bucket, key)] = hits
            if len(self._hits) > 50_000:  # keep memory bounded under abuse
                self._hits = defaultdict(list, {k: v for k, v in self._hits.items() if v and now - v[-1] < 600})
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter(LIMITS)


def client_key(request) -> str:
    """Who to rate-limit: the anonymous session if there is one, else the client address."""
    sid = request.session.get("sid") if "session" in request.scope else None
    return sid or (request.client.host if request.client else "unknown")
