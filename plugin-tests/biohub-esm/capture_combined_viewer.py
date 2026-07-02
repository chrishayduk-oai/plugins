#!/usr/bin/env python3
"""Capture a ready combined-demo viewer through the official Playwright CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TEST_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TEST_ROOT.parents[1] / "plugins" / "biohub-esm"
sys.path.insert(0, str(TEST_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from package_combined_evidence import (
    load_strict_json,
    png_dimensions,
    validate_local_viewer_url,
)
from biohub_esm_lib.errors import ValidationError
from biohub_esm_lib.provenance import sha256_file, utc_now, write_json_atomic
from biohub_esm_lib.security import redact_text


def browser_environment() -> dict[str, str]:
    allowed = (
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env["NO_COLOR"] = "1"
    for name in ("ESM_API_KEY", "HF_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        env.pop(name, None)
    return env


def run_cli(
    pwcli: Path,
    arguments: list[str],
    *,
    operation: str,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 90,
) -> dict[str, Any]:
    completed = subprocess.run(
        [str(pwcli), *arguments],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    record = {
        "operation": operation,
        "returncode": completed.returncode,
        "stdout": redact_text(completed.stdout),
        "stderr": redact_text(completed.stderr),
    }
    if completed.returncode != 0:
        raise ValidationError(
            f"Playwright CLI {operation} failed with exit {completed.returncode}"
        )
    return record


def capture(
    *,
    run_dir: Path,
    source_url: str,
    pwcli: Path,
    width: int,
    height: int,
    ready_timeout: float,
    session: str,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    pwcli = pwcli.resolve()
    source_url = validate_local_viewer_url(source_url)
    if shutil.which("npx") is None:
        raise ValidationError("npx is required by the Playwright CLI skill wrapper")
    if not pwcli.is_file() or not os.access(pwcli, os.X_OK):
        raise ValidationError("Playwright CLI wrapper is missing or not executable")
    if not (run_dir / "viewer.html").is_file():
        raise ValidationError("combined-demo viewer.html is missing")
    screenshot = run_dir / "screenshot.png"
    observation_path = run_dir / "browser-observation.json"
    capture_path = run_dir / "screenshot-capture.json"
    config_path = run_dir / "playwright-cli.json"
    for output in (screenshot, observation_path, capture_path, config_path):
        if output.exists():
            raise ValidationError(f"refusing to overwrite browser evidence: {output.name}")
    if width <= 0 or height <= 0 or ready_timeout <= 0:
        raise ValidationError("viewport and ready timeout must be positive")

    env = browser_environment()
    commands: list[dict[str, Any]] = []
    write_json_atomic(
        config_path,
        {
            "browser": {
                "browserName": "chromium",
                "launchOptions": {"headless": True},
            }
        },
    )
    version = run_cli(pwcli, ["--version"], operation="version", cwd=run_dir, env=env)
    commands.append(version)
    tool_version = version["stdout"].strip()
    if not tool_version:
        raise ValidationError("Playwright CLI returned no version")
    prefix = [f"--session={session}"]
    opened = False
    try:
        commands.append(
            run_cli(
                pwcli,
                [*prefix, "open", source_url, f"--config={config_path.name}"],
                operation="open",
                cwd=run_dir,
                env=env,
            )
        )
        opened = True
        commands.append(
            run_cli(
                pwcli,
                [*prefix, "snapshot", "--depth=2"],
                operation="snapshot",
                cwd=run_dir,
                env=env,
            )
        )
        commands.append(
            run_cli(
                pwcli,
                [*prefix, "resize", str(width), str(height)],
                operation="resize",
                cwd=run_dir,
                env=env,
            )
        )
        expression = (
            "() => ({url: window.location.href, "
            "ready_state: document.body.dataset.ready ?? null, "
            "error: document.body.dataset.error ?? null, "
            "viewport: {width: window.innerWidth, height: window.innerHeight}})"
        )
        deadline = time.monotonic() + ready_timeout
        observation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            commands.append(
                run_cli(
                    pwcli,
                    [
                        *prefix,
                        "eval",
                        expression,
                        f"--filename={observation_path.name}",
                    ],
                    operation="eval",
                    cwd=run_dir,
                    env=env,
                )
            )
            observation = load_strict_json(observation_path)
            if observation.get("ready_state") == "error" or observation.get("error") is not None:
                raise ValidationError("combined-demo viewer reported a browser render error")
            if observation.get("ready_state") == "true":
                break
            time.sleep(0.5)
        if observation is None or observation.get("ready_state") != "true":
            raise ValidationError("combined-demo viewer did not become ready before timeout")
        if observation != {
            "url": source_url,
            "ready_state": "true",
            "error": None,
            "viewport": {"width": width, "height": height},
        }:
            raise ValidationError("browser observation does not match the requested viewer")
        commands.append(
            run_cli(
                pwcli,
                [*prefix, "screenshot", f"--filename={screenshot.name}"],
                operation="screenshot",
                cwd=run_dir,
                env=env,
            )
        )
        if png_dimensions(screenshot) != (width, height):
            raise ValidationError("Playwright screenshot dimensions do not match the viewport")
    finally:
        if opened:
            commands.append(
                run_cli(
                    pwcli,
                    [*prefix, "close"],
                    operation="close",
                    cwd=run_dir,
                    env=env,
                )
            )

    record = {
        "schema_version": "1.0",
        "tool": "@playwright/cli",
        "tool_version": tool_version,
        "source_url": source_url,
        "browser_config": {
            "path": config_path.name,
            "sha256": sha256_file(config_path),
        },
        "browser_observation": {
            "path": observation_path.name,
            "sha256": sha256_file(observation_path),
        },
        "screenshot": {
            "path": screenshot.name,
            "sha256": sha256_file(screenshot),
            "width": width,
            "height": height,
        },
        "captured_at": utc_now(),
        "commands": commands,
    }
    write_json_atomic(capture_path, record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument(
        "--pwcli",
        default=str(Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh"),
    )
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--ready-timeout", type=float, default=90)
    parser.add_argument("--session", default="lsc110-combined-evidence")
    args = parser.parse_args()
    try:
        record = capture(
            run_dir=Path(args.run_dir),
            source_url=args.source_url,
            pwcli=Path(args.pwcli),
            width=args.width,
            height=args.height,
            ready_timeout=args.ready_timeout,
            session=args.session,
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "tool": record["tool"],
                    "tool_version": record["tool_version"],
                    "source_url": record["source_url"],
                    "screenshot_sha256": record["screenshot"]["sha256"],
                },
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
