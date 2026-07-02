#!/usr/bin/env python3
"""Attach a verified browser capture and package allowlisted combined evidence."""

from __future__ import annotations

import argparse
import binascii
import json
import os
import struct
import sys
import tarfile
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from biohub_esm_lib.errors import ValidationError
from biohub_esm_lib.provenance import (
    artifact_record,
    sha256_file,
    utc_now,
    validate_provenance,
    write_bytes_atomic,
    write_json_atomic,
)


def load_strict_json(path: Path) -> dict[str, Any]:
    def reject(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"expected JSON object: {path}")
    return value


def png_dimensions(path: Path) -> tuple[int, int]:
    """Fully parse, CRC-check, and decode the simple RGB/RGBA browser PNG."""

    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValidationError("combined-demo screenshot is not a PNG")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    saw_end = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValidationError("combined-demo screenshot has a truncated PNG chunk")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise ValidationError("combined-demo screenshot has a truncated PNG chunk")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        if binascii.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ValidationError("combined-demo screenshot has a corrupt PNG chunk")
        chunks.append((kind, payload))
        offset = chunk_end
        if kind == b"IEND":
            saw_end = True
            break
    if not saw_end or offset != len(data):
        raise ValidationError("combined-demo screenshot has an invalid PNG ending")
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise ValidationError("combined-demo screenshot lacks a valid PNG IHDR")
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", chunks[0][1]
    )
    if (
        width <= 0
        or height <= 0
        or bit_depth != 8
        or color_type not in {2, 6}
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        raise ValidationError("combined-demo screenshot uses an unsupported PNG encoding")
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    if not compressed:
        raise ValidationError("combined-demo screenshot has no PNG image data")
    try:
        pixels = zlib.decompress(compressed)
    except zlib.error as exc:
        raise ValidationError("combined-demo screenshot PNG image data is corrupt") from exc
    row_size = width * (3 if color_type == 2 else 4)
    if len(pixels) != height * (row_size + 1):
        raise ValidationError("combined-demo screenshot PNG pixel data is incomplete")
    if any(pixels[row * (row_size + 1)] > 4 for row in range(height)):
        raise ValidationError("combined-demo screenshot has an invalid PNG row filter")
    return width, height


def validate_local_viewer_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("screenshot source URL must be a string")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("screenshot source URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/viewer.html"
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("screenshot source must be an exact local viewer URL")
    return value


def validate_utc_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("browser capture timestamp must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("browser capture timestamp must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationError("browser capture timestamp must be a UTC timestamp")
    return value


def validate_browser_capture(
    *, capture_path: Path, run_dir: Path, screenshot: Path, width: int, height: int
) -> dict[str, Any]:
    capture_path = capture_path.resolve()
    if capture_path.parent != run_dir or capture_path.name != "screenshot-capture.json":
        raise ValidationError("capture record must be <run-dir>/screenshot-capture.json")
    capture = load_strict_json(capture_path)
    required = {
        "schema_version",
        "tool",
        "tool_version",
        "source_url",
        "browser_config",
        "browser_observation",
        "screenshot",
        "captured_at",
        "commands",
    }
    if set(capture) != required or capture["schema_version"] != "1.0":
        raise ValidationError("browser capture record schema is invalid")
    if capture["tool"] != "@playwright/cli" or not isinstance(
        capture["tool_version"], str
    ) or not capture["tool_version"].strip():
        raise ValidationError("browser capture must identify @playwright/cli and its version")
    source_url = validate_local_viewer_url(capture["source_url"])

    config_record = capture["browser_config"]
    if not isinstance(config_record, dict) or set(config_record) != {"path", "sha256"}:
        raise ValidationError("Playwright browser-config artifact record is invalid")
    config_path = (run_dir / str(config_record["path"])).resolve()
    if config_path.parent != run_dir or config_path.name != "playwright-cli.json":
        raise ValidationError("Playwright config must be <run-dir>/playwright-cli.json")
    if sha256_file(config_path) != config_record["sha256"]:
        raise ValidationError("Playwright config checksum does not match its record")
    if load_strict_json(config_path) != {
        "browser": {
            "browserName": "chromium",
            "launchOptions": {"headless": True},
        }
    }:
        raise ValidationError("Playwright config must select isolated headless Chromium")

    observation_record = capture["browser_observation"]
    if not isinstance(observation_record, dict) or set(observation_record) != {
        "path",
        "sha256",
    }:
        raise ValidationError("browser observation artifact record is invalid")
    observation_path = (run_dir / str(observation_record["path"])).resolve()
    if observation_path.parent != run_dir or observation_path.name != "browser-observation.json":
        raise ValidationError("browser observation must be <run-dir>/browser-observation.json")
    if sha256_file(observation_path) != observation_record["sha256"]:
        raise ValidationError("browser observation checksum does not match its record")
    observation = load_strict_json(observation_path)
    if set(observation) != {"url", "ready_state", "error", "viewport"}:
        raise ValidationError("browser observation schema is invalid")
    if observation["url"] != source_url or observation["ready_state"] != "true":
        raise ValidationError("browser did not observe the ready local viewer URL")
    if observation["error"] is not None:
        raise ValidationError("browser observed a viewer error state")
    if observation["viewport"] != {"width": width, "height": height}:
        raise ValidationError("browser-observed viewport does not match the screenshot")

    screenshot_record = capture["screenshot"]
    if not isinstance(screenshot_record, dict) or screenshot_record != {
        "path": "screenshot.png",
        "sha256": sha256_file(screenshot),
        "width": width,
        "height": height,
    }:
        raise ValidationError("browser capture screenshot identity does not match the PNG")
    validate_utc_timestamp(capture["captured_at"])
    commands = capture["commands"]
    required_operations = {"open", "snapshot", "resize", "eval", "screenshot"}
    allowed_operations = required_operations | {"version", "close"}
    operations = {
        item.get("operation") for item in commands if isinstance(item, dict)
    } if isinstance(commands, list) else set()
    if (
        not isinstance(commands, list)
        or not required_operations.issubset(operations)
        or not operations.issubset(allowed_operations)
    ):
        raise ValidationError("browser capture command evidence is incomplete")
    for command in commands:
        if set(command) != {"operation", "returncode", "stdout", "stderr"}:
            raise ValidationError("browser capture command evidence schema is invalid")
        if command["returncode"] != 0 or not isinstance(command["stdout"], str) or not isinstance(
            command["stderr"], str
        ):
            raise ValidationError("browser capture command evidence records a failure")
    return capture


def verified_allowlisted_artifacts(
    provenance: dict[str, Any], run_dir: Path
) -> list[Path]:
    run_dir = run_dir.resolve()
    paths: list[Path] = []
    basenames: set[str] = set()
    for record in provenance["artifacts"]:
        path = Path(record["path"]).resolve()
        if path.parent != run_dir:
            raise ValidationError("combined evidence artifact escaped its run directory")
        if not path.is_file():
            raise ValidationError(f"combined evidence artifact is missing: {path.name}")
        if (
            path.stat().st_size != record["size_bytes"]
            or sha256_file(path) != record["sha256"]
        ):
            raise ValidationError(f"combined evidence artifact checksum drifted: {path.name}")
        if path.name in basenames:
            raise ValidationError("combined evidence artifact basenames must be unique")
        basenames.add(path.name)
        paths.append(path)
    return paths


def attach_screenshot(
    *,
    run_dir: Path,
    screenshot: Path,
    capture_record: Path,
    expected_width: int,
    expected_height: int,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    screenshot = screenshot.resolve()
    if screenshot.parent != run_dir or screenshot.name != "screenshot.png":
        raise ValidationError("screenshot must be <run-dir>/screenshot.png")
    width, height = png_dimensions(screenshot)
    if (width, height) != (expected_width, expected_height):
        raise ValidationError(
            f"screenshot is {width}x{height}; expected {expected_width}x{expected_height}"
        )
    capture = validate_browser_capture(
        capture_path=capture_record,
        run_dir=run_dir,
        screenshot=screenshot,
        width=width,
        height=height,
    )

    provenance_path = run_dir / "provenance.json"
    provenance = load_strict_json(provenance_path)
    validate_provenance(provenance)
    existing = verified_allowlisted_artifacts(provenance, run_dir)
    if screenshot in existing:
        raise ValidationError("screenshot is already attached to provenance")
    viewer = run_dir / "viewer.html"
    if viewer not in existing:
        raise ValidationError("viewer.html is not in the workflow artifact allowlist")

    screenshot_record = artifact_record(screenshot, media_type="image/png")
    capture_path = capture_record.resolve()
    capture_record = artifact_record(capture_path, media_type="application/json")
    observation_path = run_dir / "browser-observation.json"
    config_path = run_dir / "playwright-cli.json"
    observation_record = artifact_record(observation_path, media_type="application/json")
    config_artifact = artifact_record(config_path, media_type="application/json")
    observation = load_strict_json(observation_path)
    provenance["artifacts"].extend(
        [screenshot_record, capture_record, observation_record, config_artifact]
    )
    provenance["parameters"]["viewer_capture"] = {
        "tool": capture["tool"],
        "tool_version": capture["tool_version"],
        "source_url": capture["source_url"],
        "ready_state": observation["ready_state"],
        "viewport": {"width": width, "height": height},
        "captured_at": capture["captured_at"],
    }
    provenance["finished_at"] = capture["captured_at"]
    validate_provenance(provenance)
    write_json_atomic(provenance_path, provenance)
    verified_allowlisted_artifacts(provenance, run_dir)
    return provenance


def package(
    *, run_dir: Path, evidence_dir: Path, asset_output: Path
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    evidence_dir = evidence_dir.resolve()
    asset_output = asset_output.resolve()
    if asset_output.exists():
        raise ValidationError("refusing to overwrite an existing plugin screenshot asset")
    if evidence_dir.exists() and any(evidence_dir.iterdir()):
        raise ValidationError("combined evidence output directory must be empty")
    provenance_path = run_dir / "provenance.json"
    provenance = load_strict_json(provenance_path)
    validate_provenance(provenance)
    artifact_paths = verified_allowlisted_artifacts(provenance, run_dir)
    screenshot = run_dir / "screenshot.png"
    if screenshot not in artifact_paths:
        raise ValidationError("screenshot is missing from the provenance allowlist")

    members = sorted(path.name for path in artifact_paths) + ["provenance.json"]
    summary = load_strict_json(run_dir / "workflow-summary.json")
    evidence_dir.parent.mkdir(parents=True, exist_ok=True)
    asset_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".combined-evidence-", dir=evidence_dir.parent
    ) as staging_name:
        staging = Path(staging_name)
        archive_path = staging / "artifacts.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in sorted(artifact_paths, key=lambda value: value.name):
                archive.add(path, arcname=path.name, recursive=False)
            archive.add(provenance_path, arcname=provenance_path.name, recursive=False)
        staged_archive_record = artifact_record(
            archive_path, media_type="application/gzip"
        )
        readme = f"""# Combined live-demonstration evidence

The workflow folded a deterministic novel sequence with managed
`{summary['fold']['model']}`, derived Atlas SAE feature annotations, searched
related proteins, traversed a representative cluster, aligned two returned
structures, and rendered the results in the NGL Structure Viewer. The browser
capture was taken only after `document.body.dataset.ready == 'true'`; an
`error` state is a hard failure.

This is integration evidence, not an experimental or functional claim. The
fold is a static model hypothesis; Atlas similarity and learned-feature labels
require independent validation.

- normalized input SHA-256: `{summary['input_sha256']}`
- mean pLDDT: `{summary['fold']['mean_plddt']}`
- pTM: `{summary['fold']['ptm']}`
- archive: `{archive_path.name}`
- archive size: `{staged_archive_record['size_bytes']}` bytes
- archive SHA-256: `{staged_archive_record['sha256']}`
- screenshot SHA-256: `{sha256_file(screenshot)}`
- allowlisted archive members: {len(members)}

Provenance paths retain their absolute live-run locations. After extraction,
relocate by archive-member basename and verify the recorded SHA-256; checksums
are the portable identity.

Atlas-derived JSON, structures, accessions, and annotations came from the
[public ESM Atlas API](https://biohub.ai/esm/protein/atlas/api-docs/overview.html)
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Aligned
structures and the workflow summary are adaptations; retain attribution and
identify changes. Released ESM code/weights are MIT licensed at their pinned
revisions.
"""
        write_bytes_atomic(staging / "README.md", readme.encode("utf-8"))
        if evidence_dir.exists():
            evidence_dir.rmdir()
        os.replace(staging, evidence_dir)
    write_bytes_atomic(asset_output, screenshot.read_bytes())
    archive_path = evidence_dir / "artifacts.tar.gz"
    archive_record = artifact_record(archive_path, media_type="application/gzip")
    return {
        "archive": archive_record,
        "members": members,
        "screenshot_asset": artifact_record(asset_output, media_type="image/png"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--screenshot", required=True)
    parser.add_argument("--capture-record", required=True)
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--asset-output", required=True)
    args = parser.parse_args()
    provenance = attach_screenshot(
        run_dir=Path(args.run_dir),
        screenshot=Path(args.screenshot),
        capture_record=Path(args.capture_record),
        expected_width=args.viewport_width,
        expected_height=args.viewport_height,
    )
    result = package(
        run_dir=Path(args.run_dir),
        evidence_dir=Path(args.evidence_dir),
        asset_output=Path(args.asset_output),
    )
    result["provenance_execution_route"] = provenance["execution_route"]
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
