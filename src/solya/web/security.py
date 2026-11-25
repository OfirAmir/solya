"""Signed cookies, CSRF tokens, proxy-aware client IP and a sliding-window rate limiter."""
from __future__ import annotations

import hashlib
import hmac
from collections import defaultdict, deque
from collections.abc import Callable

from itsdangerous import BadData, TimestampSigner, URLSafeTimedSerializer

from solya.clock import Clock, SystemClock

SESSION_COOKIE = "solya_s"
ADMIN_COOKIE = "solya_admin"
SESSION_MAX_AGE = 30 * 24 * 3600
ADMIN_MAX_AGE = 12 * 3600


def cookie_params(public_base_url: str, max_age: int) -> dict:
    """Keyword arguments for Response.set_cookie."""
    return {
        "max_age": max_age,
        "httponly": True,
        "samesite": "lax",
        "secure": str(public_base_url).startswith("https://"),
    }


class Sessions:
    def __init__(
        self, secret: bytes, clock: Clock | None = None, admin_password: str | None = None
    ) -> None:
        self._secret = secret
        clock = clock or SystemClock()

        class _Signer(TimestampSigner):
            def get_timestamp(self) -> int:
                return int(clock.now().timestamp())

        self._serializer = URLSafeTimedSerializer(
            secret, salt="solya-session", signer=_Signer
        )
        # the salt covers the admin password: changing it invalidates admin sessions
        pw = hashlib.sha256((admin_password or "").encode()).hexdigest()
        self._admin_serializer = URLSafeTimedSerializer(
            secret, salt=f"solya-admin:{pw}", signer=_Signer
        )

    def issue(self, owner_id: int) -> str:
        return self._serializer.dumps({"o": owner_id})

    def read(self, cookie: str | None) -> int | None:
        if not cookie:
            return None
        try:
            data = self._serializer.loads(cookie, max_age=SESSION_MAX_AGE)
        except BadData:
            return None
        owner_id = data.get("o") if isinstance(data, dict) else None
        return owner_id if isinstance(owner_id, int) else None

    def issue_admin(self) -> str:
        return self._admin_serializer.dumps({"a": True})

    def read_admin(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        try:
            data = self._admin_serializer.loads(cookie, max_age=ADMIN_MAX_AGE)
        except BadData:
            return False
        return isinstance(data, dict) and data.get("a") is True

    def csrf_token(self, cookie_value: str) -> str:
        return hmac.new(
            self._secret, b"csrf:" + cookie_value.encode(), hashlib.sha256
        ).hexdigest()

    def check_csrf(self, cookie_value: str | None, token: str | None) -> bool:
        if not cookie_value or not token:
            return False
        return hmac.compare_digest(self.csrf_token(cookie_value).encode(), token.encode())


def client_ip(request) -> str:
    """The peer address. Behind a proxy, uvicorn's ProxyHeadersMiddleware (serve:
    proxy_headers=True, forwarded_allow_ips) has already rewritten it from
    X-Forwarded-For for trusted peers; XFF is never parsed here."""
    return request.client.host if request.client else "unknown"


class RateLimiter:
    def __init__(self, limit: int, window_s: int, clock: Clock | Callable[[], float]) -> None:
        self.limit = limit
        self.window_s = window_s
        self._now = (lambda: clock.now().timestamp()) if hasattr(clock, "now") else clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        """True when the key is already at its limit; records nothing."""
        now = self._now()
        q = self._hits[key]
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return len(q) >= self.limit

    def hit(self, key: str) -> bool:
        """Record an attempt; False when the key is over the limit."""
        now = self._now()
        q = self._hits[key]
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
