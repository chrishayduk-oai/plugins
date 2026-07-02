from __future__ import annotations

import binascii
import struct
import sys
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from package_combined_evidence import attach_screenshot, package
from biohub_esm_lib.provenance import (
    artifact_record,
    build_provenance,
    validate_provenance,
    write_json_atomic,
)
from biohub_esm_lib.errors import ValidationError


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + b"\x20\x40\x60" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def browser_capture(
    run_dir: Path,
    screenshot: Path,
    *,
    source_url: str = "http://127.0.0.1:8000/viewer.html",
    observation_updates: dict[str, Any] | None = None,
) -> Path:
    config_path = run_dir / "playwright-cli.json"
    write_json_atomic(
        config_path,
        {
            "browser": {
                "browserName": "chromium",
                "launchOptions": {"headless": True},
            }
        },
    )
    observation = {
        "url": source_url,
        "ready_state": "true",
        "error": None,
        "viewport": {"width": 1440, "height": 900},
    }
    observation.update(observation_updates or {})
    observation_path = run_dir / "browser-observation.json"
    write_json_atomic(observation_path, observation)
    capture_path = run_dir / "screenshot-capture.json"
    write_json_atomic(
        capture_path,
        {
            "schema_version": "1.0",
            "tool": "@playwright/cli",
            "tool_version": "0.1.15",
            "source_url": source_url,
            "browser_config": {
                "path": config_path.name,
                "sha256": artifact_record(config_path)["sha256"],
            },
            "browser_observation": {
                "path": observation_path.name,
                "sha256": artifact_record(observation_path)["sha256"],
            },
            "screenshot": {
                "path": screenshot.name,
                "sha256": artifact_record(screenshot)["sha256"],
                "width": 1440,
                "height": 900,
            },
            "captured_at": "2026-07-01T00:00:02Z",
            "commands": [
                {
                    "operation": operation,
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                }
                for operation in ("open", "snapshot", "resize", "eval", "screenshot")
            ],
        },
    )
    return capture_path


class CombinedEvidencePackagingTests(unittest.TestCase):
    def test_only_provenance_allowlist_is_packaged_with_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            evidence_dir = root / "evidence"
            asset = root / "asset.png"
            run_dir.mkdir()
            viewer = run_dir / "viewer.html"
            viewer.write_text("<html></html>")
            summary = run_dir / "workflow-summary.json"
            write_json_atomic(
                summary,
                {
                    "input_sha256": "a" * 64,
                    "fold": {"model": "model", "mean_plddt": 0.8, "ptm": 0.7},
                },
            )
            (run_dir / "stale.txt").write_text("must not package")
            provenance = build_provenance(
                route="atlas-api",
                endpoint="https://biohub.ai/esm/protein/api/v1alpha1",
                model_id=None,
                model_revision=None,
                inputs="MKT",
                parameters={},
                seed=None,
                started_at="2026-07-01T00:00:00Z",
                finished_at="2026-07-01T00:00:01Z",
                artifacts=[
                    artifact_record(viewer, media_type="text/html"),
                    artifact_record(summary, media_type="application/json"),
                ],
            )
            write_json_atomic(run_dir / "provenance.json", provenance)
            screenshot = run_dir / "screenshot.png"
            screenshot.write_bytes(png(1440, 900))
            capture_path = browser_capture(run_dir, screenshot)
            attached = attach_screenshot(
                run_dir=run_dir,
                screenshot=screenshot,
                capture_record=capture_path,
                expected_width=1440,
                expected_height=900,
            )
            validate_provenance(attached)
            result = package(
                run_dir=run_dir, evidence_dir=evidence_dir, asset_output=asset
            )
            with tarfile.open(evidence_dir / "artifacts.tar.gz") as archive:
                names = archive.getnames()
            self.assertIn("screenshot.png", names)
            self.assertIn("screenshot-capture.json", names)
            self.assertIn("browser-observation.json", names)
            self.assertIn("playwright-cli.json", names)
            self.assertNotIn("stale.txt", names)
            self.assertEqual(asset.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(result["archive"]["media_type"], "application/gzip")

    def test_rejects_truncated_png_and_unobserved_ready_state(self) -> None:
        for mutation in ("truncated", "not-ready", "error"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                (run_dir / "viewer.html").write_text("<html></html>")
                screenshot = run_dir / "screenshot.png"
                screenshot.write_bytes(
                    png(1440, 900)[:33] if mutation == "truncated" else png(1440, 900)
                )
                updates = (
                    {"ready_state": None}
                    if mutation == "not-ready"
                    else ({"ready_state": "error", "error": "render failed"} if mutation == "error" else {})
                )
                capture_path = browser_capture(
                    run_dir, screenshot, observation_updates=updates
                )
                with self.assertRaises(ValidationError):
                    attach_screenshot(
                        run_dir=run_dir,
                        screenshot=screenshot,
                        capture_record=capture_path,
                        expected_width=1440,
                        expected_height=900,
                    )

    def test_rejects_localhost_url_parser_bypasses(self) -> None:
        bypasses = (
            "http://127.0.0.1:@evil.example/viewer.html",
            "http://127.0.0.1:8000/other.html",
            "http://127.0.0.1:8000/viewer.html?next=evil",
            "https://127.0.0.1:8000/viewer.html",
        )
        for source_url in bypasses:
            with self.subTest(source_url=source_url), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                (run_dir / "viewer.html").write_text("<html></html>")
                screenshot = run_dir / "screenshot.png"
                screenshot.write_bytes(png(1440, 900))
                capture_path = browser_capture(
                    run_dir, screenshot, source_url=source_url
                )
                with self.assertRaises(ValidationError):
                    attach_screenshot(
                        run_dir=run_dir,
                        screenshot=screenshot,
                        capture_record=capture_path,
                        expected_width=1440,
                        expected_height=900,
                    )

    def test_existing_asset_failure_leaves_evidence_output_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            viewer = run_dir / "viewer.html"
            viewer.write_text("<html></html>")
            summary = run_dir / "workflow-summary.json"
            write_json_atomic(
                summary,
                {
                    "input_sha256": "a" * 64,
                    "fold": {"model": "model", "mean_plddt": 0.8, "ptm": 0.7},
                },
            )
            provenance = build_provenance(
                route="atlas-api",
                endpoint="https://biohub.ai/esm/protein/api/v1alpha1",
                model_id=None,
                model_revision=None,
                inputs="MKT",
                parameters={},
                seed=None,
                started_at="2026-07-01T00:00:00Z",
                finished_at="2026-07-01T00:00:01Z",
                artifacts=[
                    artifact_record(viewer, media_type="text/html"),
                    artifact_record(summary, media_type="application/json"),
                ],
            )
            write_json_atomic(run_dir / "provenance.json", provenance)
            screenshot = run_dir / "screenshot.png"
            screenshot.write_bytes(png(1440, 900))
            capture_path = browser_capture(run_dir, screenshot)
            attach_screenshot(
                run_dir=run_dir,
                screenshot=screenshot,
                capture_record=capture_path,
                expected_width=1440,
                expected_height=900,
            )
            asset = root / "asset.png"
            asset.write_bytes(b"existing")
            evidence_dir = root / "evidence"
            with self.assertRaises(ValidationError):
                package(
                    run_dir=run_dir,
                    evidence_dir=evidence_dir,
                    asset_output=asset,
                )
            self.assertFalse(evidence_dir.exists())
            self.assertEqual(asset.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
