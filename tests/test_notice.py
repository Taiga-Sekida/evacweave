"""Terms Article 5(4): third-party OSS licenses must be stated in the submission."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _required_packages() -> set[str]:
    names = set()
    for path in ROOT.glob("requirements-*.txt"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", line)
            assert match, line
            names.add(_normalise(match.group(0)))
    return names


def test_notice_names_every_declared_dependency():
    notice = _normalise((ROOT / "NOTICE.md").read_text(encoding="utf-8"))
    required = _required_packages()
    assert {"fastapi", "pydantic", "google-genai", "pytest", "torch"} <= required
    # Whole-name match: "torch" must not be satisfied by "torchao".
    missing = sorted(
        name for name in required if not re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", notice)
    )
    assert not missing, f"add these to NOTICE.md with their license: {missing}"


def test_notice_states_rights_and_license_status():
    notice = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    for needle in ("第6条", "未選定", "MPL-2.0", "Gemma"):
        assert needle in notice


def test_list_licenses_script_reports_installed_packages():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "list_licenses.py")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = {line.split("\t")[0].lower(): line.split("\t") for line in out.splitlines()}
    assert rows["pydantic"][2] == "MIT"
    assert all(len(row) == 3 for row in rows.values())
