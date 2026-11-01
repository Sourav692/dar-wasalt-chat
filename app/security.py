"""Abuse and browser-safety controls: rate limits, body cap, input checks, security headers."""

import hashlib
import re
import secrets
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse

# Control characters other than tab/newline/CR, and bidi overrides that can disguise text.
# Zero-width joiners (U+200C/D) are allowed: Arabic and Persian text uses them.
FORBIDDEN_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‪-‮⁦-⁩]")

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def client_ip(request: Request, trusted_hops: int) -> str:
    """The client address as seen by our trusted proxy.

    Google's front end appends the address it saw to X-Forwarded-For, so the right-most
    `trusted_hops` entries are trustworthy and anything to their left is client-supplied.
    """
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",")]
    forwarded = [p for p in forwarded if p]
    if trusted_hops and len(forwarded) >= trusted_hops:
        return forwarded[-trusted_hops]
    return request.client.host if request.client else "unknown"


class IPHasher:
    """Salted hash so logs can group requests by client without storing the address."""

    def __init__(self, salt: str | None):
        # No configured salt: a random one per process, so hashes are unlinkable after restart.
        self.salt = (salt or secrets.token_hex(16)).encode()

    def __call__(self, ip: str) -> str:
        return hashlib.sha256(self.salt + ip.encode()).hexdigest()[:16]


class RateLimiter:
    """Sliding windows per client key: `per_minute` and `per_day`.

    ponytail: in-memory and per instance, so with Cloud Run max 2 instances a client can get
    up to 2x the limit. Move to Redis/Memorystore if that matters.
    """

    MAX_KEYS = 50_000

    def __init__(self, per_minute: int, per_day: int, clock=time.monotonic):
        self.limits = ((60.0, per_minute), (86_400.0, per_day))
        self.hits: dict[str, deque[float]] = {}
        self.clock = clock

    def check(self, key: str) -> float | None:
        """Record a hit and return None, or return seconds to wait if a limit is reached."""
        now = self.clock()
        hits = self.hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.limits[-1][0]:
            hits.popleft()
        for window, limit in self.limits:
            recent = [t for t in hits if now - t < window]
            if len(recent) >= limit:
                return round(window - (now - recent[0]), 1)
        hits.append(now)
        if len(self.hits) > self.MAX_KEYS:
            self.prune(now)
        return None

    def prune(self, now: float) -> None:
        day = self.limits[-1][0]
        for key in [k for k, v in self.hits.items() if not v or now - v[-1] >= day]:
            del self.hits[key]


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    """ASGI middleware: reject request bodies over `max_bytes`, declared or streamed."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        if not declared.isdigit() or int(declared) > self.max_bytes:
            return await self.reject(scope, receive, send)
        received = 0

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        try:
            await self.app(scope, counted, send)
        except BodyTooLarge:
            await self.reject(scope, receive, send)

    @staticmethod
    async def reject(scope, receive, send):
        response = JSONResponse({"detail": "Request body too large."}, status_code=413)
        await response(scope, receive, send)


class SecurityHeaders:
    """ASGI middleware: add SECURITY_HEADERS to every HTTP response (streaming included)."""

    def __init__(self, app):
        self.app = app
        self.headers = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *self.headers]
            await send(message)

        await self.app(scope, receive, with_headers)
