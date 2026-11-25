from datetime import timedelta
from types import SimpleNamespace

from solya.web.security import RateLimiter, Sessions, client_ip, cookie_params

SECRET = b"s" * 32


def test_session_roundtrip_and_tamper(clock):
    s = Sessions(SECRET, clock)
    c = s.issue(7)
    assert s.read(c) == 7
    assert s.read(c[:-2] + "xx") is None
    assert s.read(None) is None
    assert Sessions(b"o" * 32, clock).read(c) is None
    assert s.read_admin(c) is False  # a user cookie is not an admin cookie
    assert s.read(s.issue_admin()) is None


def test_session_expiry(clock):
    s = Sessions(SECRET, clock)
    c, a = s.issue(7), s.issue_admin()
    clock.advance(timedelta(hours=13))
    assert s.read_admin(a) is False
    assert s.read(c) == 7
    clock.advance(timedelta(days=30))
    assert s.read(c) is None


def test_admin_roundtrip(clock):
    s = Sessions(SECRET, clock)
    assert s.read_admin(s.issue_admin()) is True
    assert s.read_admin(None) is False


def test_csrf_bound_to_session(clock):
    s = Sessions(SECRET, clock)
    a, b = s.issue(1), s.issue(2)
    t = s.csrf_token(a)
    assert s.check_csrf(a, t)
    assert not s.check_csrf(b, t)
    assert not s.check_csrf(a, None)
    assert not s.check_csrf(None, t)


def test_client_ip_trusted_proxy():
    from web_helpers import proxied_client

    def ip(trusted, peer, xff):
        app = _ip_app()
        r = proxied_client(app, trusted, peer).get("/", headers={"x-forwarded-for": xff})
        return r.text

    assert ip(["127.0.0.1"], "127.0.0.1", "1.2.3.4") == "1.2.3.4"
    # proxy appended the real client; the spoofed left entry is ignored
    assert ip(["127.0.0.1"], "127.0.0.1", "9.9.9.9, 1.2.3.4") == "1.2.3.4"
    assert ip(["127.0.0.1"], "8.8.8.8", "1.2.3.4") == "8.8.8.8"
    assert client_ip(SimpleNamespace(client=None)) == "unknown"


def _ip_app():
    from fastapi import FastAPI, Request, Response

    app = FastAPI()

    @app.get("/")
    def root(request: Request):
        return Response(client_ip(request))

    return app


def test_csrf_non_ascii_rejected(clock):
    s = Sessions(SECRET, clock)
    c = s.issue(1)
    t = s.csrf_token(c)
    assert s.check_csrf(c, "é" * len(t)) is False
    tampered = ("0" if t[0] != "0" else "1") + t[1:]
    assert s.check_csrf(c, tampered) is False


def test_admin_cookie_invalidated_by_password_change(clock):
    a = Sessions(SECRET, clock, admin_password="one")
    c = a.issue_admin()
    assert a.read_admin(c) is True
    assert Sessions(SECRET, clock, admin_password="two").read_admin(c) is False


def test_trusted_proxies_from_env(monkeypatch):
    from solya.config import Settings

    base = dict(_env_file=None, solaredge_client_id="a", solaredge_client_secret="b",
                telegram_bot_token="1:t", telegram_bot_username="u",
                public_base_url="https://x.y", token_encryption_key="k")
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1, 10.0.0.5")
    assert Settings(**base).trusted_proxies == ["127.0.0.1", "10.0.0.5"]
    monkeypatch.setenv("TRUSTED_PROXIES", '["10.0.0.9"]')
    assert Settings(**base).trusted_proxies == ["10.0.0.9"]


def test_cookie_params_secure_only_on_https():
    assert cookie_params("https://x.y", 5)["secure"] is True
    assert cookie_params("http://x.y", 5)["secure"] is False
    assert cookie_params("https://x.y", 5)["httponly"] is True


def test_rate_limiter_window(clock):
    rl = RateLimiter(3, 60, clock)
    assert [rl.hit("a") for _ in range(4)] == [True, True, True, False]
    assert rl.hit("b") is True
    clock.advance(timedelta(seconds=61))
    assert rl.hit("a") is True


def test_read_admin_not_before(clock):
    s = Sessions(SECRET, clock, admin_password="pw")
    cookie = s.issue_admin()
    assert s.read_admin(cookie, not_before=None) is True
    assert s.read_admin(cookie, not_before=clock.now() - timedelta(seconds=1)) is True
    assert s.read_admin(cookie, not_before=clock.now()) is False  # issued at or before
    clock.advance(timedelta(seconds=5))
    assert s.read_admin(s.issue_admin(), not_before=clock.now() - timedelta(seconds=1)) is True


def test_rate_limiter_blocked_leaves_no_entries(clock):
    rl = RateLimiter(2, 60, clock)
    assert rl.blocked("fresh") is False
    assert "fresh" not in rl._hits
    rl.hit("a")
    clock.advance(timedelta(seconds=61))
    assert rl.blocked("a") is False
    assert "a" not in rl._hits
    rl.hit("b")
    clock.advance(timedelta(seconds=61))
    assert rl.hit("b") is True and len(rl._hits["b"]) == 1
