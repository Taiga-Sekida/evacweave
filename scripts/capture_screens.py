"""Capture reproducible EvacWeave screen PNGs from a running app.

Writes 12 PNGs (the ten slides, the play page and the architecture diagram) and a
manifest.json with each image's source and SHA-256.

Example:
  python -m pip install playwright
  python -m playwright install chromium
  python scripts/capture_screens.py --base-url http://127.0.0.1:8000
  python scripts/capture_screens.py \
    --base-url https://disaster-agentic-mvp-66ttczdcza-an.a.run.app

No private Cloud Run credentials, model calls, or user data are involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_SOURCE = ROOT / "docs" / "architecture" / "evacweave-architecture.html"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 10_000  # anything smaller is treated as a blank or failed screenshot

SCENES = (
    ("01-project-introduction.png", "概要／合成データを用いた紹介", "conceptual"),
    ("02-running-demo.png", "公開画面／実動作", "computed"),
    ("03-blocked-route.png", "道路閉鎖／不成立", "computed"),
    ("04-rejection-reasons.png", "失敗理由の記録", "computed"),
    ("05-alternative-route.png", "代替経路／成立", "computed"),
    ("06-arrival-timeline.png", "2名の移動履歴", "computed"),
    ("07-verification-boundary.png", "エージェントの構成と判定の分離", "architecture"),
    ("08-cloud-security.png", "公開・非公開サービス", "architecture"),
    ("09-agent-trace.png", "自律エージェントの実行記録（ルール方策）", "agent-trace-rule-policy"),
    ("10-training-roadmap.png", "Gemmaの学習・推論構想", "roadmap-not-deployed"),
)


def validate_url(url: str) -> str:
    parsed = urlsplit(url.rstrip("/"))
    localhost = parsed.hostname in {"localhost", "127.0.0.1"}
    hosted = parsed.scheme == "https" and parsed.hostname in {
        "disaster-agentic-mvp-571915385983.asia-northeast1.run.app",
        "disaster-agentic-mvp-66ttczdcza-an.a.run.app",
    }
    if (
        not (hosted or (localhost and parsed.scheme == "http"))
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("use the published Cloud Run MVP URL or local HTTP loopback")
    return url.rstrip("/")


def _read_png(path: Path, error_message: str) -> bytes:
    """The screenshot's bytes, or RuntimeError(error_message) if it is not a real PNG."""
    raw = path.read_bytes()
    if len(raw) < MIN_PNG_BYTES or raw[:8] != PNG_MAGIC:
        raise RuntimeError(error_message)
    return raw


def _check_deployment(base_url: str, expected_revision: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The deployment's /health and /v1/showcase, refusing a stale or inconsistent one."""
    # base_url passed validate_url(): loopback HTTP or the published Cloud Run origin.
    with urllib.request.urlopen(base_url + "/health", timeout=15) as response:  # noqa: S310
        health: dict[str, Any] = json.load(response)
    if health.get("status") != "ok":
        raise RuntimeError("public MVP health check failed")
    if expected_revision is not None and health.get("revision") != expected_revision:
        raise RuntimeError("deployed revision does not match expected revision")

    with urllib.request.urlopen(base_url + "/v1/showcase", timeout=20) as response:  # noqa: S310
        evidence: dict[str, Any] = json.load(response)
    if (
        evidence.get("schema_version") != "evacweave-showcase/v2"
        or evidence.get("live_ai_used") is not False
        or evidence.get("baseline_result", {}).get("status") != "FAIL"
        or evidence.get("repaired_result", {}).get("status") != "PASS"
        or len(evidence.get("probes", [])) < 1
        or evidence.get("agent_trace", {}).get("policy") != "rule-policy"
    ):
        raise RuntimeError("computed showcase evidence is missing or inconsistent")
    return health, evidence


def _capture_slides(page: Page, base_url: str, output: Path) -> list[dict[str, Any]]:
    files = []
    for i, (filename, caption, kind) in enumerate(SCENES, start=1):
        page.goto(
            base_url + "/slides?shot=" + str(i),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.locator('body[data-ready="true"]').wait_for(timeout=30000)
        panel = page.locator("#shot-" + str(i))
        panel.wait_for(state="visible")
        path = output / filename
        page.screenshot(
            path=str(path),
            full_page=True,
            animations="disabled",
        )
        raw = _read_png(path, "invalid screenshot: " + filename)
        files.append(
            {
                "number": i,
                "filename": filename,
                "caption": caption,
                "evidence_class": kind,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "source_url": base_url + "/slides?shot=" + str(i),
            }
        )
        print(f"[{i:02}/{len(SCENES)}] {filename}")
    return files


def _capture_play_page(page: Page, base_url: str, output: Path) -> dict[str, Any]:
    """The play page, as a visitor first sees it (no roads closed yet)."""
    page.goto(base_url + "/", wait_until="domcontentloaded", timeout=30000)
    page.locator('body[data-ready="true"]').wait_for(timeout=30000)
    play_path = output / "00-play.png"
    page.screenshot(path=str(play_path), full_page=True, animations="disabled")
    play_raw = _read_png(play_path, "invalid screenshot: 00-play.png")
    print("[00] 00-play.png")
    return {
        "number": 0,
        "filename": play_path.name,
        "caption": "プレイ画面（初期状態）",
        "evidence_class": "computed",
        "sha256": hashlib.sha256(play_raw).hexdigest(),
        "size_bytes": len(play_raw),
        "source_url": base_url + "/",
    }


def _capture_architecture(page: Page, output: Path) -> dict[str, Any]:
    # The diagram is a self-contained 1600x900 page (no network, no scripts).
    page.set_viewport_size({"width": 1600, "height": 900})
    page.set_content(ARCHITECTURE_SOURCE.read_text(encoding="utf-8"), wait_until="load")
    architecture_image = output / "architecture-diagram.png"
    page.screenshot(
        path=str(architecture_image),
        clip={"x": 0, "y": 0, "width": 1600, "height": 900},
        animations="disabled",
        timeout=20000,
    )
    arch_raw = _read_png(architecture_image, "invalid architecture diagram screenshot")
    print("[ARCH] architecture-diagram.png")
    return {
        "filename": architecture_image.name,
        "source_file": str(ARCHITECTURE_SOURCE.relative_to(ROOT)),
        "sha256": hashlib.sha256(arch_raw).hexdigest(),
        "size_bytes": len(arch_raw),
        "evidence_class": "implementation-status-architecture",
    }


def _write_manifest(
    output: Path,
    base_url: str,
    health: dict[str, Any],
    evidence: dict[str, Any],
    files: list[dict[str, Any]],
    architecture_record: dict[str, Any],
) -> None:
    manifest = {
        "schema_version": "evacweave-submission-capture/v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "source": base_url,
        "deployed_revision": health.get("revision", "unknown"),
        "scenario_id": evidence["scenario"]["id"],
        "baseline_digest": evidence["baseline_result"]["input_digest"],
        "repair_digest": evidence["repaired_result"]["input_digest"],
        "agent_outcome": evidence["agent_trace"]["outcome"],
        "recorded_ai_run_status": evidence["recorded_ai_run_status"],
        "disclaimer": "Synthetic simulator evidence; not real-world safety guidance.",
        "images": files,
        "architecture": architecture_record,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        type=validate_url,
        help="Cloud Run MVP URL or http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--out",
        default="artifacts/screens",
        help="output directory for PNG files and manifest",
    )
    parser.add_argument(
        "--expected-revision",
        default=None,
        help="optional expected /health revision; fail instead of capturing stale deployment",
    )
    args = parser.parse_args()

    health, evidence = _check_deployment(args.base_url, args.expected_revision)

    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as play:
        browser = play.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1600, "height": 900},
            device_scale_factor=1,
            locale="ja-JP",
            reduced_motion="reduce",
        )
        try:
            files = _capture_slides(page, args.base_url, output)
            files.insert(0, _capture_play_page(page, args.base_url, output))
            architecture_record = _capture_architecture(page, output)
        finally:
            browser.close()

    _write_manifest(output, args.base_url, health, evidence, files, architecture_record)
    print("Saved:", output)


if __name__ == "__main__":
    main()
