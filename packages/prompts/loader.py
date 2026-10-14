"""Versioned prompt loader with a fixed allowlist."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PROMPTS = {
    ("resident", "v1"): _ROOT / "resident" / "v1_system.txt",
    ("orchestrator", "v2"): _ROOT / "orchestrator" / "v2_system.txt",
    ("report_reader", "v1"): _ROOT / "report_reader" / "v1_system.txt",
    ("care_reader", "v1"): _ROOT / "care_reader" / "v1_system.txt",
    ("care_reader", "v2"): _ROOT / "care_reader" / "v2_system.txt",
}


def load_prompt(name: str, version: str) -> str:
    try:
        path = _PROMPTS[(name, version)]
    except KeyError as exc:
        raise ValueError("unknown prompt name/version") from exc
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("prompt file is empty")
    return text
