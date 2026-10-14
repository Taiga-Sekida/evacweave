"""The live-Gemini daily count in Cloud Storage: no network, Cloud Storage is faked."""

import io
import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

import apps.api.live_agent as live
import apps.api.live_quota as quota
from apps.api.main import app
from apps.http import NoRedirect
from tests.test_live_agent import ORIGIN, _trace_bytes

DAY = date(2026, 10, 14)
BUCKET = "physical-ai-guardian-evacweave-live-quota"
client = TestClient(app)


class FakeStorage:
    """Cloud Storage's JSON API for one bucket: media download and upload with ifGenerationMatch."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail: Exception | None = None
        self.race = 0  # how many writes lose to another writer first

    def send(self, request):
        url = urlsplit(request.full_url)
        self.requests.append((request.get_method(), request.full_url))
        assert url.scheme == "https" and url.hostname == "storage.googleapis.com"
        assert request.get_header("Authorization") == "Bearer access-token"
        if self.fail is not None:
            raise self.fail
        if request.get_method() == "GET":
            prefix = f"/storage/v1/b/{BUCKET}/o/"
            assert url.path.startswith(prefix) and parse_qs(url.query) == {"alt": ["media"]}
            name = unquote(url.path[len(prefix) :])
            if name not in self.objects:
                raise HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())
            body, generation = self.objects[name]
            return body, str(generation)
        assert url.path == f"/upload/storage/v1/b/{BUCKET}/o"
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        name, expected = query["name"], int(query["ifGenerationMatch"])
        assert query["uploadType"] == "media"
        current = self.objects.get(name, (b"", 0))[1]
        if self.race:
            self.race -= 1
            self.objects[name] = (
                json.dumps({"used": json.loads(request.data)["used"]}).encode(),
                current + 1,
            )
            raise HTTPError(request.full_url, 412, "Precondition Failed", {}, io.BytesIO())
        if expected != current:
            raise HTTPError(request.full_url, 412, "Precondition Failed", {}, io.BytesIO())
        self.objects[name] = (request.data, current + 1)
        return b"{}", str(current + 1)

    def used(self, day=DAY):
        return json.loads(self.objects[quota.object_name(day)][0])["used"]


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(quota, "_send", fake.send)
    monkeypatch.setattr(quota, "_access_token", lambda: "access-token")
    return fake


def test_the_count_survives_a_new_instance(storage):
    first = quota.StoredDailyQuota(BUCKET)
    assert [first.take(3, DAY), first.take(3, DAY)] == [2, 1]
    # A new instance starts with nothing in memory but reads the same count.
    second = quota.StoredDailyQuota(BUCKET)
    assert second.left(3, DAY) == 1
    assert second.take(3, DAY) == 0
    assert second.take(3, DAY) is None
    assert storage.used() == 3


def test_a_full_day_writes_nothing_and_the_next_day_starts_again(storage):
    stored = quota.StoredDailyQuota(BUCKET)
    assert stored.take(1, DAY) == 0
    writes = len(storage.requests)
    assert stored.take(1, DAY) is None
    assert [m for m, _ in storage.requests[writes:]] == ["GET"]
    assert stored.take(1, date(2026, 10, 15)) == 0
    assert storage.objects.keys() == {"live-quota/2026-10-14.json", "live-quota/2026-10-15.json"}


def test_the_first_write_of_a_day_requires_that_no_object_exists(storage):
    quota.StoredDailyQuota(BUCKET).take(5, DAY)
    method, url = storage.requests[-1]
    assert method == "POST" and parse_qs(urlsplit(url).query)["ifGenerationMatch"] == ["0"]


def test_a_lost_race_reads_again_and_never_takes_past_the_limit(storage):
    stored = quota.StoredDailyQuota(BUCKET)
    stored.take(2, DAY)
    storage.race = 1  # another writer takes the last request first
    assert stored.take(2, DAY) is None
    assert storage.used() == 2


def test_a_count_that_keeps_changing_refuses(storage):
    storage.race = quota.WRITE_ATTEMPTS
    with pytest.raises(quota.QuotaUnavailable):
        quota.StoredDailyQuota(BUCKET).take(100, DAY)


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("u", 403, "Forbidden", {}, io.BytesIO()),
        HTTPError("u", 503, "Unavailable", {}, io.BytesIO()),
        URLError("no route"),
        TimeoutError(),
    ],
)
def test_an_unreachable_count_refuses(storage, failure):
    storage.fail = failure
    stored = quota.StoredDailyQuota(BUCKET)
    with pytest.raises(quota.QuotaUnavailable):
        stored.take(3, DAY)
    with pytest.raises(quota.QuotaUnavailable):
        stored.left(3, DAY)


@pytest.mark.parametrize(
    "body", [b"not json", b'{"used": -1}', b'{"used": "2"}', b'{"used": true}', b"{}", b" " * 2000]
)
def test_a_malformed_count_refuses(storage, body):
    storage.objects[quota.object_name(DAY)] = (body, 7)
    with pytest.raises(quota.QuotaUnavailable):
        quota.StoredDailyQuota(BUCKET).take(3, DAY)


def test_a_missing_access_token_refuses(storage, monkeypatch):
    def no_token():
        raise ValueError("metadata server returned no access token")

    monkeypatch.setattr(quota, "_access_token", no_token)
    with pytest.raises(quota.QuotaUnavailable):
        quota.StoredDailyQuota(BUCKET).take(3, DAY)
    assert storage.requests == []


@pytest.mark.parametrize("name", ["", "Bucket", "a", "x/../y", "gs://bucket", "bucket?alt=media", "-bucket"])
def test_only_bucket_names_reach_the_url(name):
    with pytest.raises(ValueError):
        quota.StoredDailyQuota(name)


# --- the public endpoints with the stored count -------------------------------------------


@pytest.fixture
def stored_live(monkeypatch, storage):
    monkeypatch.setenv("LIVE_AI_ENABLED", "1")
    monkeypatch.setenv("ORCHESTRATOR_URL", ORIGIN)
    monkeypatch.setenv("LIVE_AI_DAILY_RUNS", "2")
    monkeypatch.setenv("LIVE_AI_QUOTA_BUCKET", BUCKET)
    monkeypatch.setenv("K_SERVICE", "disaster-agentic-mvp")
    monkeypatch.setattr(live, "QUOTA", live.DailyQuota())
    monkeypatch.setattr(live, "_today", lambda: DAY)
    calls = []

    def fake_post(origin, body, trace_header):
        calls.append(body)
        return _trace_bytes(body)

    monkeypatch.setattr(live, "_post_run", fake_post)
    return calls


def _run():
    return client.post("/v1/agent/live", json={"scenario": "base"})


def test_cloud_run_counts_live_runs_in_storage(stored_live, storage):
    assert client.get("/v1/agent/live").json()["runs_left_today"] == 2
    assert _run().json()["runs_left_today"] == 1
    assert storage.used() == 1
    # Memory is not what counts: a fresh in-memory quota changes nothing.
    live.QUOTA = live.DailyQuota()
    assert _run().status_code == 200
    assert _run().status_code == 429
    assert len(stored_live) == 2
    assert client.get("/v1/agent/live").json()["runs_left_today"] == 0


def test_an_unreachable_count_refuses_before_any_call(stored_live, storage):
    storage.fail = HTTPError("u", 403, "Forbidden", {}, io.BytesIO())
    assert _run().status_code == 503
    assert stored_live == []
    status = client.get("/v1/agent/live").json()
    assert (status["enabled"], status["runs_left_today"]) == (True, 0)


def test_cloud_run_without_a_bucket_keeps_live_ai_off(stored_live, monkeypatch):
    monkeypatch.delenv("LIVE_AI_QUOTA_BUCKET")
    assert client.get("/v1/agent/live").json()["enabled"] is False
    assert _run().status_code == 503
    assert stored_live == []


def test_a_bad_bucket_name_keeps_live_ai_off(stored_live, monkeypatch):
    monkeypatch.setenv("LIVE_AI_QUOTA_BUCKET", "../other")
    assert client.get("/v1/agent/live").json()["enabled"] is False
    assert stored_live == []


# --- the scripts that create the bucket and pass its name -----------------------------------


def _script(name):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts" / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_bucket_is_private_and_only_the_public_identity_may_write_it():
    setup = _script("setup_live_quota.sh")
    assert "--uniform-bucket-level-access" in setup and "--public-access-prevention" in setup
    assert "gcloud storage buckets add-iam-policy-binding" in setup
    assert "roles/storage.objectUser" in setup
    assert "projects add-iam-policy-binding" not in setup  # no project-wide role
    assert "allUsers" not in setup and "allAuthenticatedUsers" not in setup
    default = "${PROJECT}-evacweave-live-quota"
    assert f'BUCKET="${{LIVE_AI_QUOTA_BUCKET:-{default}}}"' in setup
    assert quota.BUCKET_NAME.fullmatch("physical-ai-guardian-evacweave-live-quota")


def test_live_ai_is_deployed_only_with_its_bucket():
    deploy = _script("deploy_cloud_run_mvp.sh")
    assert 'LIVE_AI_QUOTA_BUCKET="${LIVE_AI_QUOTA_BUCKET:-${GCP_PROJECT_ID}-evacweave-live-quota}"' in deploy
    live_branch = deploy.split('if [[ "${LIVE_AI_ENABLED}" == "1" ]]; then', 1)[1].split("elif", 1)[0]
    assert "./scripts/setup_live_quota.sh" in live_branch
    assert ",ORCHESTRATOR_URL=${ORCHESTRATOR_URL},LIVE_AI_QUOTA_BUCKET=${LIVE_AI_QUOTA_BUCKET}" in deploy


class _Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_access_token_comes_from_the_metadata_server_only(monkeypatch):
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, request.get_header("Metadata-flavor"), timeout))
            return _Response(b'{"access_token": "ya29.fake", "expires_in": 3599}')

    monkeypatch.setattr(quota, "_OPENER", Opener())
    assert quota._access_token() == "ya29.fake"
    assert seen == [(quota.METADATA_TOKEN_URL, "Google", quota.TIMEOUT_S)]
    monkeypatch.setattr(
        quota, "_OPENER", type("O", (), {"open": lambda self, r, timeout: _Response(b"{}")})()
    )
    with pytest.raises(ValueError):
        quota._access_token()


def test_send_returns_the_body_and_generation(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return _Response(b'{"used": 4}', {"x-goog-generation": "17"})

    monkeypatch.setattr(quota, "_OPENER", Opener())
    assert quota._send(quota.Request("https://storage.googleapis.com/x")) == (b'{"used": 4}', "17")


def test_redirects_are_never_followed():
    assert NoRedirect().redirect_request() is None


@pytest.mark.parametrize("failure", [URLError("reset"), HTTPError("u", 403, "Forbidden", {}, io.BytesIO())])
def test_a_failed_write_refuses(storage, monkeypatch, failure):
    real_send = storage.send

    def send(request):
        if request.get_method() == "POST":
            raise failure
        return real_send(request)

    monkeypatch.setattr(quota, "_send", send)
    with pytest.raises(quota.QuotaUnavailable):
        quota.StoredDailyQuota(BUCKET).take(3, DAY)
