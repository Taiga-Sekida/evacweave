"""Daily count of live Gemini requests, kept where a new instance cannot reset it.

The public service scales to zero, so a count in an instance's memory starts again at
0 whenever Cloud Run starts a new instance: waiting for that would give anyone another
day's worth of paid calls. On Cloud Run the count is therefore one small Cloud Storage
object per UTC day (live-quota/YYYY-MM-DD.json, {"used": n}), written with a
generation precondition so two writers cannot both take the last request. The public
identity holds roles/storage.objectUser on that bucket only
(scripts/setup_live_quota.sh).

Any failure to read or write the count refuses the request: it is a spend guard, so
it fails closed. A request taken is never refunded: it may have been billed.

Without a bucket (local development and tests) the count stays in memory.
"""

from __future__ import annotations

import json
import re
from datetime import date
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request

from apps.http import NO_REDIRECT_OPENER as _OPENER

STORAGE_API = "https://storage.googleapis.com"
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"  # noqa: S105
)
# Cloud Storage bucket names; the name goes into a URL path, so nothing else is accepted.
BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]$")
TIMEOUT_S = 5.0
WRITE_ATTEMPTS = 3
MAX_OBJECT_BYTES = 1024


class QuotaUnavailable(Exception):
    """The stored count could not be read or written; the request must be refused."""


class DailyQuota:
    """The count in this instance's memory: for local development and tests only."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._used = 0

    def _used_on(self, day: date) -> int:
        if day != self._day:
            self._day, self._used = day, 0
        return self._used

    def left(self, limit: int, day: date) -> int:
        return max(0, limit - self._used_on(day))

    def take(self, limit: int, day: date) -> int | None:
        """Requests left after taking one, or None when none was left."""
        used = self._used_on(day)
        if used >= limit:
            return None
        self._used = used + 1
        return limit - self._used


class StoredDailyQuota:
    """The same count in Cloud Storage, shared by every instance of the service."""

    def __init__(self, bucket: str) -> None:
        if not BUCKET_NAME.fullmatch(bucket):
            raise ValueError("not a Cloud Storage bucket name")
        self.bucket = bucket

    def left(self, limit: int, day: date) -> int:
        used, _ = _read(self.bucket, object_name(day))
        return max(0, limit - used)

    def take(self, limit: int, day: date) -> int | None:
        """Requests left after taking one, or None when none was left."""
        name = object_name(day)
        for _ in range(WRITE_ATTEMPTS):
            used, generation = _read(self.bucket, name)
            if used >= limit:
                return None
            if _write(self.bucket, name, used + 1, generation):
                return limit - used - 1
        raise QuotaUnavailable("the stored count kept changing")


def object_name(day: date) -> str:
    return f"live-quota/{day.isoformat()}.json"


def _access_token() -> str:
    """An OAuth access token of the service's own identity, from the metadata server."""
    request = Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with _OPENER.open(request, timeout=TIMEOUT_S) as response:
        payload = json.loads(response.read(8192))
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("metadata server returned no access token")
    return token


def _send(request: Request) -> tuple[bytes, str | None]:
    """Body and generation of a 2xx response; urllib raises HTTPError for anything else."""
    with _OPENER.open(request, timeout=TIMEOUT_S) as response:
        body: bytes = response.read(MAX_OBJECT_BYTES + 1)
        return body, response.headers.get("x-goog-generation")


def _read(bucket: str, name: str) -> tuple[int, int]:
    """(used, generation) of the day's object; (0, 0) while it does not exist yet."""
    url = f"{STORAGE_API}/storage/v1/b/{bucket}/o/{quote(name, safe='')}?alt=media"
    try:
        request = Request(url, headers={"Authorization": f"Bearer {_access_token()}"})  # noqa: S310
        raw, generation = _send(request)
    except HTTPError as exc:
        if exc.code == 404:
            return 0, 0
        raise QuotaUnavailable(f"read failed: HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise QuotaUnavailable(f"read failed: {type(exc).__name__}") from exc
    try:
        used = json.loads(raw)["used"] if len(raw) <= MAX_OBJECT_BYTES else None
        stored_generation = int(generation or "0")
    except (ValueError, TypeError, KeyError) as exc:
        raise QuotaUnavailable("stored count is malformed") from exc
    if type(used) is not int or used < 0 or stored_generation <= 0:
        raise QuotaUnavailable("stored count is malformed")
    return used, stored_generation


def _write(bucket: str, name: str, used: int, generation: int) -> bool:
    """Store the count if the object is still at generation (0: does not exist); False if not."""
    query = urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": generation})
    try:
        request = Request(  # noqa: S310 - fixed API origin
            f"{STORAGE_API}/upload/storage/v1/b/{bucket}/o?{query}",
            data=json.dumps({"used": used}).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
        )
        _send(request)
    except HTTPError as exc:
        if exc.code == 412:  # another writer got there first: read again
            return False
        raise QuotaUnavailable(f"write failed: HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise QuotaUnavailable(f"write failed: {type(exc).__name__}") from exc
    return True
