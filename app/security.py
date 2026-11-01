"""Request-level protection: rate limits, security headers and a same-origin check on state-changing requests.

The app runs as ONE process (see architecturedoc.md), so an in-memory limiter is enough and needs no Redis.
If we ever run several workers, each gets its own buckets; limits then apply per worker.
"""
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse

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
    """Who to rate-limit: the logged-in account (however many sessions it opens), else the client address.
    The address is only a fallback for logged-out requests (login); behind a proxy it can be spoofed."""
    uid = request.session.get("uid") if "session" in request.scope else None
    return f"user:{uid}" if uid else "addr:" + (request.client.host if request.client else "unknown")


# ---------------- headers ----------------
# No inline scripts anywhere (confirm dialogs and tracking live in /static). Inline style attributes are used
# for the score bars, so style-src allows them; that is low risk compared with inline script.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://telegram.org",
    "frame-src https://oauth.telegram.org",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://telegram.org https://*.telegram.org https://t.me",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])


def add_headers(response, https: bool) -> None:
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")   # Telegram login opens a popup
    if https:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


# ---------------- cross-site request protection ----------------
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXEMPT_PATHS = {"/telegram/webhook"}   # called by Telegram's servers; authenticated by its secret header


def same_origin(request, base_url: str) -> bool:
    """Reject state-changing requests that a browser marks as coming from another site.
    Defence in depth on top of SameSite=Lax session cookies; requests without these headers (curl, tests,
    old browsers) fall back to the cookie rules."""
    if request.method in SAFE_METHODS or request.url.path in EXEMPT_PATHS:
        return True
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    host = urlparse(origin).netloc
    return host in {request.url.netloc, urlparse(base_url).netloc}


MAX_BODY = 6_000_000   # largest legitimate body: a 5 MB listing CSV upload
