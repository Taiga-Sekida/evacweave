"""Per-client limits on the public API: a flood from one address is told to wait."""

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
import apps.api.rate_limit as rate_limit
from apps.api.rate_limit import API, LIVE_AI, LIVE_AI_ALL, LIVE_AI_DAY, Limit, SlidingWindow, client_of

client = TestClient(main.app)


def test_a_window_lets_its_requests_through_then_says_how_long_to_wait():
    window = SlidingWindow()
    limit = Limit("t", 3, 10.0)
    assert [window.wait("a", limit, now=t) for t in (0.0, 1.0, 2.0)] == [0.0, 0.0, 0.0]
    assert window.wait("a", limit, now=3.0) == pytest.approx(7.0)
    assert window.wait("b", limit, now=3.0) == 0.0  # another client has its own count
    assert window.wait("a", limit, now=10.5) == 0.0  # the first request left the window


def test_the_window_forgets_idle_clients_first_and_never_grows_past_its_bound(monkeypatch):
    monkeypatch.setattr(rate_limit, "MAX_CLIENTS", 4)
    window = SlidingWindow()
    limit = Limit("t", 1, 10.0)
    for index in range(20):
        window.wait(f"c{index}", limit, now=float(index))
        assert len(window._hits) <= 4


@pytest.mark.parametrize(
    ("forwarded", "peer", "expected"),
    [
        ("203.0.113.7", None, "203.0.113.7"),
        # Anything before the last entry is the client's own claim.
        ("1.2.3.4, 203.0.113.7", None, "203.0.113.7"),
        # A home line gets a /56: every address in it is one client.
        ("2001:db8:1:2:aaaa::1", None, "2001:db8:1::/56"),
        ("2001:db8:1:ff:bbbb::9", None, "2001:db8:1::/56"),
        (None, "198.51.100.2", "198.51.100.2"),
        ("not-an-address", None, "not-an-address"),
        ("", None, "unknown"),
    ],
)
def test_the_client_is_the_address_the_front_end_saw(forwarded, peer, expected):
    assert client_of(forwarded, peer) == expected


@pytest.fixture
def limited(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    main.LIMITER.clear()
    yield
    main.LIMITER.clear()


def test_off_the_cloud_nothing_is_limited(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    main.LIMITER.clear()
    assert all(client.get("/v1/agent/live").status_code == 200 for _ in range(API.requests + 5))


def test_on_cloud_run_the_limit_is_on_without_asking(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.setenv("K_SERVICE", "disaster-agentic-mvp")
    assert main._rate_limits_on()


def test_a_flood_from_one_address_gets_429_with_retry_after(limited):
    me = {"X-Forwarded-For": "203.0.113.7"}
    assert all(client.get("/v1/agent/live", headers=me).status_code == 200 for _ in range(API.requests))
    refused = client.get("/v1/agent/live", headers=me)
    assert refused.status_code == 429
    assert refused.json() == {"detail": "too many requests"}
    assert 1 <= int(refused.headers["Retry-After"]) <= API.window_s
    assert refused.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in refused.headers
    # Another address, the pages and the health check are not affected.
    assert client.get("/v1/agent/live", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 200
    assert client.get("/", headers=me).status_code == 200
    assert client.get("/health", headers=me).status_code == 200


def test_gemini_requests_have_a_tighter_limit_than_the_rest(limited):
    me = {"X-Forwarded-For": "203.0.113.8"}
    statuses = [
        client.post("/v1/agent/care", json={"scenario": "river_district"}, headers=me).status_code
        for _ in range(LIVE_AI.requests + 1)
    ]
    # Live AI is off in tests (503); the limit answers before the endpoint is reached.
    assert statuses[:-1] == [503] * LIVE_AI.requests and statuses[-1] == 429
    # The rule-only planner is still there for the same address.
    assert client.get("/v1/agent/care?scenario=river_district", headers=me).status_code == 200


def test_a_load_balancer_of_ours_in_front_is_skipped_when_configured():
    assert client_of("1.2.3.4, 203.0.113.7, 10.0.0.9", None, proxies=1) == "203.0.113.7"
    assert client_of("10.0.0.9", "198.51.100.2", proxies=1) == "198.51.100.2"


def test_one_client_cannot_spend_the_days_gemini_runs_and_nor_can_a_crowd(limited, monkeypatch):
    clock = iter(float(t) * 700 for t in range(1000))  # each request in a new 10-minute window
    real = main.LIMITER.wait
    monkeypatch.setattr(main.LIMITER, "wait", lambda who, limit: real(who, limit, now=next(clock)))
    me = {"X-Forwarded-For": "203.0.113.9"}
    answers = [
        client.post("/v1/agent/read", json={"scenario": "base", "report": "x"}, headers=me).status_code
        for _ in range(LIVE_AI_DAY.requests + 1)
    ]
    assert answers[-1] == 429 and 429 not in answers[:-1]
    main.LIMITER.clear()
    # Many addresses together, all within the same hour, still share one allowance.
    monkeypatch.setattr(main.LIMITER, "wait", lambda who, limit: real(who, limit, now=0.0))
    crowd = [
        client.post(
            "/v1/agent/read",
            json={"scenario": "base", "report": "x"},
            headers={"X-Forwarded-For": f"203.0.113.{n}"},
        ).status_code
        for n in range(LIVE_AI_ALL.requests + 1)
    ]
    assert crowd[-1] == 429 and 429 not in crowd[:-1]
