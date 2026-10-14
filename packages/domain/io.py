"""Bounded, strict JSON decoding shared by the CLI, fixtures and every HTTP service.

Invalid data is an input error, never a simulation FAIL.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, TypeVar

from .models import Contract

T = TypeVar("T", bound=Contract)
MAX_INPUT_BYTES = 1_048_576
MAX_NESTING = 64


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


def loads_strict(raw: bytes) -> Any:
    """Decode UTF-8 JSON, rejecting duplicate keys, NaN/Infinity and deep nesting."""
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except RecursionError as exc:  # json recurses before the depth check below can run
        raise ValueError("JSON nesting limit exceeded") from exc
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_NESTING:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            item.encode("utf-8")  # lone surrogates such as "\ud800" are rejected here
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("non-finite JSON number")
    return value


def parse_contract[T: Contract](raw: bytes, model: type[T]) -> T:
    """Strictly decode one JSON object into a contract (UnicodeError/ValueError on bad input)."""
    value = loads_strict(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    # JSON mode deliberately accepts JSON arrays for immutable tuple contracts.
    return model.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))


def load_contract[T: Contract](path: str | Path, model: type[T]) -> T:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds byte limit")
    return parse_contract(raw, model)
