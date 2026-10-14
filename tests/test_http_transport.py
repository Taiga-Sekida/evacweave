"""Bounded JSON transport and Cloud Run identity tokens, without network access."""

import asyncio
import io
import json
import sys
import types

import pytest

import packages.services.http_json as http_json
from packages.services.http_json import BoundedJsonTransport, GoogleCloudIdentityTokenSource


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _post(transport, url="https://gemma.example/v1/move-intent", timeout_s=5.0):
    return asyncio.run(
        transport.post_json(url, {"a": 1}, headers={"Authorization": "Bearer t"}, timeout_s=timeout_s)
    )


def test_transport_sends_json_with_headers_and_reads_bounded_json(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
        seen["body"] = json.loads(request.data)
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(http_json, "urlopen", fake_urlopen)
    assert _post(BoundedJsonTransport()) == {"ok": True}
    assert seen["body"] == {"a": 1}
    assert seen["headers"]["Authorization"] == "Bearer t"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["timeout"] == 5.0


@pytest.mark.parametrize(
    ("body", "message"),
    [(b"x" * 2_000, "byte limit"), (b"[1, 2]", "JSON object")],
)
def test_transport_rejects_oversized_or_non_object_responses(monkeypatch, body, message):
    monkeypatch.setattr(http_json, "urlopen", lambda request, timeout: FakeResponse(body))
    with pytest.raises(ValueError, match=message):
        _post(BoundedJsonTransport(max_response_bytes=1_024))


@pytest.mark.parametrize(
    ("url", "timeout_s"),
    [
        ("http://gemma.example/v1", 5.0),
        ("https://user:pw@gemma.example/v1", 5.0),
        ("file:///etc/passwd", 5.0),
        ("https://gemma.example/v1", 0.0),
        ("https://gemma.example/v1", 121.0),
    ],
)
def test_transport_refuses_unsafe_targets_before_any_io(monkeypatch, url, timeout_s):
    monkeypatch.setattr(http_json, "urlopen", lambda *a, **k: pytest.fail("must not open"))
    with pytest.raises(ValueError):
        _post(BoundedJsonTransport(), url=url, timeout_s=timeout_s)


@pytest.mark.parametrize("limit", [1_023, 1_048_577])
def test_transport_response_limit_is_bounded(limit):
    with pytest.raises(ValueError):
        BoundedJsonTransport(max_response_bytes=limit)


def _fake_google(monkeypatch, token):
    closed = []
    session = types.SimpleNamespace(close=lambda: closed.append(True))
    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.Request = lambda: types.SimpleNamespace(session=session)
    id_token = types.ModuleType("google.oauth2.id_token")
    id_token.fetch_id_token = lambda request, audience: token
    for name, module in {
        "google": types.ModuleType("google"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": requests_mod,
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.id_token": id_token,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["google.auth.transport"].requests = requests_mod
    sys.modules["google.auth"].transport = sys.modules["google.auth.transport"]
    sys.modules["google"].auth = sys.modules["google.auth"]
    sys.modules["google.oauth2"].id_token = id_token
    sys.modules["google"].oauth2 = sys.modules["google.oauth2"]
    return closed


def test_identity_token_is_fetched_for_the_service_origin(monkeypatch):
    closed = _fake_google(monkeypatch, "header.payload.sig")
    token = asyncio.run(GoogleCloudIdentityTokenSource().token("https://gemma.example/"))
    assert token == "header.payload.sig"
    assert closed == [True]  # the HTTP session is always released


@pytest.mark.parametrize("token", ["", "x" * 8193, None])
def test_identity_token_shape_is_checked(monkeypatch, token):
    _fake_google(monkeypatch, token)
    with pytest.raises(ValueError, match="identity token"):
        asyncio.run(GoogleCloudIdentityTokenSource().token("https://gemma.example"))


@pytest.mark.parametrize("audience", ["http://gemma.example", "https://gemma.example/path", "gemma"])
def test_identity_token_audience_must_be_a_service_origin(audience):
    with pytest.raises(ValueError, match="audience"):
        asyncio.run(GoogleCloudIdentityTokenSource().token(audience))
