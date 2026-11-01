import pytest
from starlette.requests import Request

from app.config import get_settings
from app.security import RateLimiter, client_ip, client_key


def request_with(xff=None, host="10.0.0.9"):
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (host, 1234)})


def test_client_ip_uses_rightmost_trusted_hop():
    # The client can put anything on the left; the front end appends what it saw.
    assert client_ip(request_with("6.6.6.6, 203.0.113.7"), trusted_hops=1) == "203.0.113.7"
    assert client_ip(request_with(), trusted_hops=1) == "10.0.0.9"
    assert client_ip(request_with("203.0.113.7"), trusted_hops=0) == "10.0.0.9"


def test_rate_limiter_minute_and_day_windows():
    now = [0.0]
    limiter = RateLimiter(per_minute=3, per_day=5, clock=lambda: now[0])
    assert [limiter.check("a") for _ in range(3)] == [None, None, None]
    wait = limiter.check("a")
    assert wait is not None and 0 < wait <= 60
    assert limiter.check("b") is None  # other clients unaffected
    now[0] = 61
    assert limiter.check("a") is None and limiter.check("a") is None
    assert limiter.check("a") > 60  # 5 per day reached; wait is hours, not seconds
    now[0] = 86_401
    assert limiter.check("a") is None


def test_rate_limiter_memory_is_capped_by_lru_eviction():
    limiter = RateLimiter(per_minute=10, per_day=10)
    limiter.MAX_KEYS = 2
    limiter.check("a")
    limiter.check("b")
    limiter.check("a")  # a is now the most recent
    limiter.check("c")
    assert list(limiter.hits) == ["a", "c"]


def test_ipv6_clients_keyed_by_64_network():
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::9")
    assert client_key("2001:db8:1:2::1") != client_key("2001:db8:1:3::1")
    assert client_key("203.0.113.7") == "203.0.113.7"
    assert client_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert client_key("not-an-ip") == "not-an-ip"


def test_global_limit_applies_across_clients(client, monkeypatch):
    import app.main as main
    from tests.test_api import fake_llm

    monkeypatch.setattr(main, "stream_chat", fake_llm("ok"))
    main.app.state.global_limiter = RateLimiter(per_minute=1, per_day=100)
    assert ask(client, "hi", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    r = ask(client, "hi", headers={"X-Forwarded-For": "198.51.100.2"})
    assert r.status_code == 429 and "busy" in r.json()["detail"]


def ask(client, content, **kw):
    return client.post("/api/chat", json={"messages": [{"role": "user", "content": content}]}, **kw)


@pytest.mark.parametrize("bad", ["hi\x00there", "bell\x07", "abc‮evil"])
def test_control_and_bidi_characters_rejected(client, bad):
    assert ask(client, bad).status_code == 422


def test_zero_width_joiner_allowed(client, monkeypatch):
    import app.main as main
    from tests.test_api import fake_llm

    monkeypatch.setattr(main, "stream_chat", fake_llm("ok"))
    assert ask(client, "می‌خواهم").status_code == 200  # Persian uses ZWNJ


def test_oversized_body_rejected(client):
    body = '{"messages": [{"role": "user", "content": "' + "x" * 140_000 + '"}]}'
    r = client.post("/api/chat", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_security_headers_on_every_response(client):
    headers = client.get("/healthz").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert client.get("/openapi.json").status_code == 404


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    import app.main as main
    from app.security import RateLimiter
    from tests.test_api import fake_llm

    monkeypatch.setattr(main, "stream_chat", fake_llm("ok"))
    main.app.state.limiter = RateLimiter(per_minute=2, per_day=100)
    assert [ask(client, "hi").status_code for _ in range(2)] == [200, 200]
    r = ask(client, "hi")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    get_settings.cache_clear()
