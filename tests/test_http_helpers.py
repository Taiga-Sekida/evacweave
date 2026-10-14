"""apps/http.read_contract with raw ASGI headers (test clients re-encode header values)."""

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.http import read_contract
from packages.domain.ai import AgentRunRequest


def _request(headers: list[tuple[bytes, bytes]], body: bytes = b"{}") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": headers}, receive)


@pytest.mark.parametrize(
    ("length", "status"),
    [
        (b"\xb2", 400),  # "²": str.isdigit() says True, int() would raise -> was a 500
        (b"\xd9\xa3", 400),  # Arabic-Indic digit three, same trap
        (b"12a", 400),
        (b"9" * 5000, 413),  # never int() a 5000-digit string
        (b"999999", 413),
    ],
)
def test_declared_length_is_validated_before_reading(length, status):
    request = _request([(b"content-type", b"application/json"), (b"content-length", length)])
    with pytest.raises(HTTPException) as error:
        asyncio.run(read_contract(request, AgentRunRequest, max_bytes=1024))
    assert error.value.status_code == status


def test_media_type_parameters_and_case_are_accepted():
    request = _request([(b"content-type", b"Application/JSON; charset=utf-8")])
    with pytest.raises(HTTPException) as error:  # passes the media type, then fails the contract
        asyncio.run(read_contract(request, AgentRunRequest, max_bytes=1024))
    assert error.value.status_code == 422
