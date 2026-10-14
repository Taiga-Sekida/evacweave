"""HTTP helpers shared by the Cloud Run services: bounded bodies, strict contracts and
outgoing requests that never follow a redirect."""

from __future__ import annotations

from typing import Any
from urllib.request import HTTPRedirectHandler, build_opener

from fastapi import HTTPException, Request
from pydantic import ValidationError

from packages.domain.io import parse_contract
from packages.domain.models import Contract

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}


async def read_contract[T: Contract](request: Request, model: type[T], *, max_bytes: int) -> T:
    """Stream at most max_bytes of JSON and strictly validate it (400/413/415/422 otherwise)."""
    # Check the media type before consuming an untrusted request body.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="application/json required")
    # Reject a declared oversized body early; the streamed limit below stays authoritative
    # when Content-Length is absent or wrong.
    declared = request.headers.get("content-length")
    if declared is not None:
        # str.isdigit() also accepts digits such as "²" that int() rejects.
        if not (declared.isascii() and declared.isdigit()):
            raise HTTPException(status_code=400, detail="invalid content-length")
        if len(declared) > 18 or int(declared) > max_bytes:
            raise HTTPException(status_code=413, detail="request body too large")
    raw = bytearray()
    async for chunk in request.stream():
        # Never buffer a chunk that would exceed the budget.
        if len(chunk) > max_bytes - len(raw):
            raise HTTPException(status_code=413, detail="request body too large")
        raw.extend(chunk)
    try:
        return parse_contract(bytes(raw), model)
    except (UnicodeError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="invalid request") from exc


class NoRedirect(HTTPRedirectHandler):
    """urllib would forward the Authorization header to a redirect target; refuse instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# For requests that carry a credential (apps/api/live_agent.py, apps/api/live_quota.py):
# a 3xx answer raises HTTPError instead of being followed. The scripts keep their own copy.
NO_REDIRECT_OPENER = build_opener(NoRedirect)
