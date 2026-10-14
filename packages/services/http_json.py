"""Bounded JSON HTTP transport and Cloud Run identity-token source.

Serves the Gemma path (gemma_provider.py), which is not wired into the deployed services
(roadmap). The public service calls the private agent service through apps/api/live_agent.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class JsonTransport(Protocol):
    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Mapping[str, Any]: ...


class IdentityTokenSource(Protocol):
    async def token(self, audience: str) -> str: ...


class BoundedJsonTransport:
    def __init__(self, max_response_bytes: int = 65_536) -> None:
        if not 1_024 <= max_response_bytes <= 1_048_576:
            raise ValueError("max_response_bytes outside allowed range")
        self.max_response_bytes = max_response_bytes

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._post_json, url, payload, dict(headers), timeout_s)

    def _post_json(
        self, url: str, payload: Mapping[str, Any], headers: dict[str, str], timeout_s: float
    ) -> Mapping[str, Any]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("model service URL must be an HTTPS origin/path without credentials")
        if not 0 < timeout_s <= 120:
            raise ValueError("timeout_s must be between 0 and 120")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(  # noqa: S310 - scheme and host validated above
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        )
        with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - URL is validated above.
            raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise ValueError("model service response exceeds byte limit")
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("model service response must be a JSON object")
        return decoded


class GoogleCloudIdentityTokenSource:
    """Fetch an ID token from ADC for private Cloud Run service-to-service calls."""

    async def token(self, audience: str) -> str:
        parsed = urlsplit(audience)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("Cloud Run audience must be an HTTPS service origin")
        return await asyncio.to_thread(self._fetch, audience.rstrip("/"))

    @staticmethod
    def _fetch(audience: str) -> str:
        # Lazy import keeps unit tests and the deterministic simulator free of Google SDKs.
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        try:
            token = google.oauth2.id_token.fetch_id_token(request, audience)
        finally:
            request.session.close()
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ValueError("invalid identity token response")
        return token
