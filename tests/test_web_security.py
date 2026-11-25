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


def _req(peer, xff=None):
    headers = {"x-forwarded-for": xff} if xff else {}
    client = SimpleNamespace(host=peer) if peer else None
    return SimpleNamespace(client=client, headers=headers)


def test_client_ip_trusted_proxy():
    trusted = ["127.0.0.1"]
    assert client_ip(_req("127.0.0.1", "1.2.3.4, 10.0.0.1"), trusted) == "1.2.3.4"
    assert client_ip(_req("9.9.9.9", "1.2.3.4"), trusted) == "9.9.9.9"
    assert client_ip(_req("127.0.0.1"), trusted) == "127.0.0.1"
    assert client_ip(_req(None), trusted) == "unknown"


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
