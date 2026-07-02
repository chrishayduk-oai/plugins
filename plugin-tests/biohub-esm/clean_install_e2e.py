#!/usr/bin/env python3
"""Install the plugin in an isolated Codex home and capture auditable prompts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.provenance import (
    canonical_json,
    sha256_bytes,
    utc_now,
    write_bytes_atomic,
    write_json_atomic,
)
from biohub_esm_lib.security import redact_text

MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "xhigh"
MARKETPLACE = "lsc110-local"
PLUGIN = "biohub-esm"
PLUGIN_SKILLS = {
    "biohub-esm",
    "biohub-esm-setup",
    "esmc",
    "esmfold2",
    "esm-atlas",
    "esmfold2-binder-design",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r'(?i)"(?:access_token|refresh_token|api_key|token_secret)"\s*:'),
    re.compile(r"(?i)(?:ESM_API_KEY|HF_TOKEN|MODAL_TOKEN_(?:ID|SECRET))\s*[=:]\s*\S+"),
)

SCENARIOS = (
    {
        "id": "01-esmc",
        "expected": "router + esmc",
        "expected_skills": ("biohub-esm", "esmc"),
        "required_terms": ("esmc-600m-2024-12", "ESM_API_KEY", "2,048"),
        "prompt": """I have one protein sequence under 100 residues and need embeddings and hidden states plus zero-shot entropy and one mutation score at one prespecified residue using exactly one masked context. Do not plan an exhaustive masking sweep or mutation campaign. This is planning only: do not call any provider, network service, or local model and do not inspect credentials. Choose the execution route and exact model ID; state the sequence/token bound, credential name by name only, provenance to retain, and the warning about fitted versus untrained classifier heads.""",
    },
    {
        "id": "02-esmfold2",
        "expected": "router + esmfold2",
        "expected_skills": ("biohub-esm", "esmfold2"),
        "required_terms": ("esmfold2-2026-05", "mmCIF", "MSA"),
        "prompt": """Plan one accuracy-priority all-atom complex fold containing a protein with an available MSA, one DNA chain, one modified RNA chain, and a ligand. This is planning only: do not call providers or inspect credentials. Choose managed versus scale-out/self-hosted execution, full versus Fast, the exact model ID, valid input checks, mmCIF/PDB/confidence outputs, provenance, and scientific limits.""",
    },
    {
        "id": "03-atlas",
        "expected": "router + esm-atlas",
        "expected_skills": ("biohub-esm", "esm-atlas"),
        "required_terms": ("v1alpha1", "800", "699", "CC BY 4.0"),
        "prompt": """Plan a public ESM Atlas workflow for a 650-residue protein: similarity search, protein-by-MD5 lookup, cluster traversal, feature catalog/detail, and a resumable batch. Do not make network calls. State whether a key is needed, conservative search and on-demand-fold limits, alpha schema handling, atomic batch state/cancel behavior, anonymous S3 role, provenance, and license.""",
    },
    {
        "id": "04-binder",
        "expected": "router + binder design",
        "expected_skills": ("biohub-esm", "esmfold2-binder-design"),
        "required_terms": ("Modal", "self-host", "1,000", "ESM_API_KEY"),
        "prompt": """Correct this request without running anything: 'Use my ESM_API_KEY to run one managed Biohub minibinder design against PD-L1 and call it a useful campaign.' Give the permitted execution routes, credential distinction, useful campaign scale, one-seed smoke limitation, cost/persistence confirmation, provenance requirements, safety boundaries, and experimental validation requirements.""",
    },
    {
        "id": "05-negative",
        "expected": "no Biohub ESM activation",
        "expected_skills": (),
        "required_terms": ("Needleman",),
        "prompt": """Explain in two sentences how Needleman-Wunsch global alignment computes edit distance for two short DNA strings. Do not use external tools.""",
    },
)
SCENARIO_EVIDENCE_FILES = {"prompt.txt", "events.jsonl", "final.txt", "run.json"}
ROOT_EVIDENCE_FILES = {"install.json", "source-tree.json"}


def source_manifest(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or "__pycache__" in relative.parts
            or path.suffix == ".pyc"
        ):
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative.as_posix(),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return {
        "root": str(root),
        "file_count": len(files),
        "tree_sha256": sha256_bytes(canonical_json(files)),
        "files": files,
        "excluded": ["**/__pycache__/**", "**/*.pyc"],
    }


def safe_text(value: str) -> str:
    sanitized = redact_text(value, env={})
    for pattern in SECRET_PATTERNS:
        if pattern.search(sanitized):
            raise RuntimeError("refusing to persist output matching a credential pattern")
    return sanitized


def contains_required_term(text: str, term: str) -> bool:
    if term.lower() in text.lower():
        return True
    normalize = lambda value: re.sub(r"[^a-z0-9]+", "", value.lower())
    return normalize(term) in normalize(text)


def safe_environment(codex_home: Path, scenario_home: Path) -> dict[str, str]:
    allowed = (
        "USER",
        "LOGNAME",
        "PATH",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["HOME"] = str(scenario_home)
    env["CODEX_HOME"] = str(codex_home)
    env["NO_COLOR"] = "1"
    return env


def run_command(
    command: list[str], *, env: dict[str, str], cwd: Path, timeout: float
) -> dict[str, Any]:
    started_at = utc_now()
    monotonic_start = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    return {
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": round(time.monotonic() - monotonic_start, 6),
        "returncode": completed.returncode,
        "stdout": safe_text(completed.stdout),
        "stderr": safe_text(completed.stderr),
    }


def require_success(record: dict[str, Any], context: str) -> None:
    if record["returncode"] != 0:
        raise RuntimeError(
            f"{context} failed with exit {record['returncode']}: {record['stderr'][:500]}"
        )


def validate_resumed_run(
    record: dict[str, Any],
    *,
    scenario_id: str,
    prompt: str,
    trace: str,
    final: str,
    source_digest: str,
) -> None:
    """Reject stale or tampered scenario evidence before resume compaction."""

    if record.get("returncode") != 0:
        raise RuntimeError(f"existing scenario {scenario_id} did not exit successfully")
    if record.get("plugin_source_tree_sha256") != source_digest:
        raise RuntimeError(
            f"existing scenario {scenario_id} was run against different plugin bytes"
        )
    payloads = {
        "prompt_sha256": prompt.encode("utf-8"),
        "events_sha256": trace.encode("utf-8"),
        "final_sha256": final.encode("utf-8"),
    }
    for field, payload in payloads.items():
        actual = hashlib.sha256(payload).hexdigest()
        if record.get(field) != actual:
            raise RuntimeError(
                f"existing scenario {scenario_id} {field} does not match its file"
            )


def find_installed_manifests(codex_home: Path, expected_digest: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for manifest_path in codex_home.rglob(".codex-plugin/plugin.json"):
        try:
            metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.get("name") != PLUGIN:
            continue
        root = manifest_path.parent.parent
        manifest = source_manifest(root)
        if manifest["tree_sha256"] == expected_digest:
            matches.append(
                {
                    "root": str(root),
                    "version": metadata.get("version"),
                    "tree_sha256": manifest["tree_sha256"],
                    "file_count": manifest["file_count"],
                }
            )
    if not matches:
        raise RuntimeError("installed plugin bytes do not match the source-tree digest")
    return matches


def build_marketplace_snapshot(destination: Path) -> Path:
    destination.mkdir(parents=True)
    snapshot_plugin = destination / "plugins" / PLUGIN
    shutil.copytree(
        PLUGIN_ROOT,
        snapshot_plugin,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    registry = json.loads(
        (REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    plugin_entries = [
        entry for entry in registry["plugins"] if entry.get("name") == PLUGIN
    ]
    if len(plugin_entries) != 1:
        raise RuntimeError("source marketplace must contain exactly one Biohub ESM entry")
    snapshot_registry = {
        "name": MARKETPLACE,
        "interface": {"displayName": "LSC-110 clean local snapshot"},
        "plugins": plugin_entries,
    }
    registry_path = destination / ".agents" / "plugins" / "marketplace.json"
    write_json_atomic(registry_path, snapshot_registry)
    return registry_path


def parse_event_counts(trace: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for line in trace.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        counts[str(event.get("type", "unknown"))] += 1
    if not counts:
        raise RuntimeError("Codex JSONL trace contained no events")
    return dict(sorted(counts.items()))


def command_execution_events(trace: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in trace.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        event_id = str(item.get("id", "unknown"))
        key = (event_id, command)
        if key not in seen:
            seen.add(key)
            events.append({"event_id": event_id, "command": command})
    return events


def command_reads_installed_skill(
    command: str, *, root: str, skill: str
) -> bool:
    root = root.rstrip("/")
    suffix = f"/skills/{skill}/SKILL.md"
    if f"{root}{suffix}" in command:
        return True
    for matched in re.finditer(
        rf"\b([A-Za-z_][A-Za-z0-9_]*)=(?:['\"])?{re.escape(root)}(?:['\"])?",
        command,
    ):
        variable = matched.group(1)
        if f"${variable}{suffix}" in command or f"${{{variable}}}{suffix}" in command:
            return True
    return False


def verify_activation_trace(
    trace: str, expected_skills: tuple[str, ...], installed_roots: list[str]
) -> list[dict[str, str]]:
    command_events = command_execution_events(trace)
    accesses_anywhere: set[str] = set()
    installed_access_map: dict[tuple[str, str], set[str]] = {}
    for event in command_events:
        command = event["command"]
        for skill in PLUGIN_SKILLS:
            if f"/skills/{skill}/SKILL.md" in command:
                accesses_anywhere.add(skill)
            for root in installed_roots:
                if command_reads_installed_skill(
                    command, root=root, skill=skill
                ):
                    installed_access_map.setdefault((skill, root), set()).add(
                        event["event_id"]
                    )
    installed_accesses = [
        {
            "skill": skill,
            "installed_root": root,
            "command_event_ids": sorted(event_ids),
        }
        for (skill, root), event_ids in sorted(installed_access_map.items())
    ]
    installed_skills = {item["skill"] for item in installed_accesses}
    if expected_skills:
        missing = sorted(set(expected_skills) - installed_skills)
        if missing:
            raise RuntimeError(
                "raw Codex trace did not access expected installed skills: "
                + ", ".join(missing)
            )
    elif accesses_anywhere:
        raise RuntimeError(
            "adjacent negative unexpectedly accessed Biohub ESM skills: "
            + ", ".join(sorted(accesses_anywhere))
        )
    return installed_accesses


def validated_evidence_members(evidence_dir: Path) -> list[tuple[Path, str]]:
    expected_root = ROOT_EVIDENCE_FILES | {scenario["id"] for scenario in SCENARIOS}
    actual_root = {path.name for path in evidence_dir.iterdir()}
    if actual_root != expected_root:
        unexpected = sorted(actual_root - expected_root)
        missing = sorted(expected_root - actual_root)
        raise RuntimeError(
            "clean evidence root differs from its allowlist"
            + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
        )
    members: list[tuple[Path, str]] = []
    for name in sorted(ROOT_EVIDENCE_FILES):
        path = evidence_dir / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"clean evidence root artifact is not a regular file: {name}")
        members.append((path, name))
    for scenario in SCENARIOS:
        scenario_id = scenario["id"]
        directory = evidence_dir / scenario_id
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"clean scenario evidence is not a directory: {scenario_id}")
        actual = {path.name for path in directory.iterdir()}
        if actual != SCENARIO_EVIDENCE_FILES:
            raise RuntimeError(
                f"clean scenario {scenario_id} differs from its artifact allowlist"
            )
        for name in sorted(SCENARIO_EVIDENCE_FILES):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(
                    f"clean scenario artifact is not a regular file: {scenario_id}/{name}"
                )
            members.append((path, f"{scenario_id}/{name}"))
    return members


def compact_evidence(evidence_dir: Path) -> dict[str, Any]:
    archive_path = evidence_dir / "artifacts.tar.gz"
    members = validated_evidence_members(evidence_dir)
    with tarfile.open(archive_path, "w:gz") as archive:
        for path, archive_name in members:
            archive.add(path, arcname=archive_name, recursive=False)
    archive_record = {
        "path": archive_path.name,
        "size_bytes": archive_path.stat().st_size,
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "media_type": "application/gzip",
        "members": [archive_name for _, archive_name in members],
    }
    for scenario in SCENARIOS:
        shutil.rmtree(evidence_dir / scenario["id"])
    for name in ROOT_EVIDENCE_FILES:
        (evidence_dir / name).unlink()
    return archive_record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    codex = Path(args.codex).resolve()
    auth_file = Path(args.auth_file).resolve()
    work_dir = Path(args.work_dir).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()
    if not codex.is_file() or not auth_file.is_file():
        raise RuntimeError("Codex executable and existing auth file are required")

    codex_home = work_dir / "codex-home"
    scenario_home = work_dir / "home"
    scenario_workspace = work_dir / "workspace"
    marketplace_root = work_dir / "marketplace"
    if args.resume:
        if not work_dir.is_dir() or not evidence_dir.is_dir():
            raise RuntimeError("resume requires existing clean work and evidence directories")
    else:
        if work_dir.exists() or evidence_dir.exists():
            raise RuntimeError("work and evidence directories must be new for a clean run")
        codex_home.mkdir(parents=True)
        scenario_home.mkdir(parents=True)
        scenario_workspace.mkdir(parents=True)
        evidence_dir.mkdir(parents=True)
        os.symlink(auth_file, codex_home / "auth.json")
    env = safe_environment(codex_home, scenario_home)

    source = source_manifest(PLUGIN_ROOT)
    if args.resume:
        recorded_source = json.loads(
            (evidence_dir / "source-tree.json").read_text(encoding="utf-8")
        )
        if recorded_source["tree_sha256"] != source["tree_sha256"]:
            raise RuntimeError("plugin source bytes changed since the clean install")
        install_records = json.loads(
            (evidence_dir / "install.json").read_text(encoding="utf-8")
        )
        version = install_records["codex_version"]
    else:
        write_json_atomic(evidence_dir / "source-tree.json", source)
        marketplace_registry = build_marketplace_snapshot(marketplace_root)
        snapshot = source_manifest(marketplace_root / "plugins" / PLUGIN)
        if snapshot["tree_sha256"] != source["tree_sha256"]:
            raise RuntimeError("clean marketplace snapshot does not match source bytes")

        version = run_command([str(codex), "--version"], env=env, cwd=REPO_ROOT, timeout=30)
        require_success(version, "Codex version")
        marketplace = run_command(
            [
                str(codex),
                "plugin",
                "marketplace",
                "add",
                str(marketplace_root),
                "--json",
            ],
            env=env,
            cwd=REPO_ROOT,
            timeout=120,
        )
        require_success(marketplace, "marketplace add")
        plugin_add = run_command(
            [str(codex), "plugin", "add", f"{PLUGIN}@{MARKETPLACE}", "--json"],
            env=env,
            cwd=REPO_ROOT,
            timeout=120,
        )
        require_success(plugin_add, "plugin add")
        plugin_list = run_command(
            [str(codex), "plugin", "list", "--available", "--json"],
            env=env,
            cwd=REPO_ROOT,
            timeout=120,
        )
        require_success(plugin_list, "plugin list")
        install_records = {
            "codex_version": version,
            "marketplace_add": marketplace,
            "plugin_add": plugin_add,
            "plugin_list": plugin_list,
            "snapshot_registry_sha256": hashlib.sha256(
                marketplace_registry.read_bytes()
            ).hexdigest(),
            "snapshot_tree_sha256": snapshot["tree_sha256"],
            "installed_matches": find_installed_manifests(
                codex_home, source["tree_sha256"]
            ),
            "auth": {
                "mechanism": "symlink-to-existing-Codex-auth-file",
                "harness_inspected_contents": False,
                "harness_copied_or_persisted_contents": False,
                "codex_consumed_in_place": True,
                "scientific_provider_credentials_inherited": False,
            },
        }
        write_json_atomic(evidence_dir / "install.json", install_records)
    installed_roots = [item["root"] for item in install_records["installed_matches"]]

    scenario_summaries: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        scenario_dir = evidence_dir / scenario["id"]
        prompt = scenario["prompt"].strip() + "\n"
        prompt_path = scenario_dir / "prompt.txt"
        final_path = scenario_dir / "final.txt"
        trace_path = scenario_dir / "events.jsonl"
        run_path = scenario_dir / "run.json"
        if args.resume and run_path.is_file():
            if prompt_path.read_text(encoding="utf-8") != prompt:
                raise RuntimeError(f"scenario {scenario['id']} prompt changed since its run")
            trace = trace_path.read_text(encoding="utf-8")
            final = final_path.read_text(encoding="utf-8")
            verify_activation_trace(trace, scenario["expected_skills"], installed_roots)
            for term in scenario["required_terms"]:
                if not contains_required_term(final, term):
                    raise RuntimeError(
                        f"existing scenario {scenario['id']} omitted required term: {term}"
                    )
            record = json.loads(run_path.read_text(encoding="utf-8"))
            validate_resumed_run(
                record,
                scenario_id=scenario["id"],
                prompt=prompt,
                trace=trace,
                final=final,
                source_digest=source["tree_sha256"],
            )
            scenario_summaries.append(
                {
                    "id": scenario["id"],
                    "expected_activation": scenario["expected"],
                    "returncode": record["returncode"],
                    "elapsed_seconds": record["elapsed_seconds"],
                    "events_sha256": record["events_sha256"],
                    "final_sha256": record["final_sha256"],
                }
            )
            continue
        if scenario_dir.exists():
            shutil.rmtree(scenario_dir)
        scenario_dir.mkdir()
        write_bytes_atomic(prompt_path, prompt.encode("utf-8"))
        command = [
            str(codex),
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--dangerously-bypass-hook-trust",
            "-s",
            "read-only",
            "-C",
            str(scenario_workspace),
            "-m",
            MODEL,
            "-c",
            f'model_reasoning_effort="{REASONING_EFFORT}"',
            "-c",
            'approval_policy="never"',
            "-o",
            str(final_path),
            prompt,
        ]
        record = run_command(command, env=env, cwd=scenario_workspace, timeout=900)
        require_success(record, f"scenario {scenario['id']}")
        trace = record.pop("stdout")
        write_bytes_atomic(trace_path, (trace.rstrip() + "\n").encode("utf-8"))
        final = safe_text(final_path.read_text(encoding="utf-8"))
        write_bytes_atomic(final_path, final.encode("utf-8"))
        accessed_skills = verify_activation_trace(
            trace, scenario["expected_skills"], installed_roots
        )
        for term in scenario["required_terms"]:
            if not contains_required_term(final, term):
                raise RuntimeError(
                    f"scenario {scenario['id']} final answer omitted required term: {term}"
                )
        record.update(
            {
                "command_template": [
                    *command[:-1],
                    "<exact prompt in prompt.txt>",
                ],
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "sandbox": "read-only",
                "expected_activation": scenario["expected"],
                "expected_skill_files": list(scenario["expected_skills"]),
                "accessed_skill_files": accessed_skills,
                "required_terms": list(scenario["required_terms"]),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "events_sha256": hashlib.sha256(trace.encode()).hexdigest(),
                "final_sha256": hashlib.sha256(final.encode()).hexdigest(),
                "event_type_counts": parse_event_counts(trace),
                "plugin_source_tree_sha256": source["tree_sha256"],
            }
        )
        write_json_atomic(run_path, record)
        scenario_summaries.append(
            {
                "id": scenario["id"],
                "expected_activation": scenario["expected"],
                "returncode": record["returncode"],
                "elapsed_seconds": record["elapsed_seconds"],
                "events_sha256": record["events_sha256"],
                "final_sha256": record["final_sha256"],
            }
        )

    archive_record = compact_evidence(evidence_dir)
    summary = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "codex_cli": version["stdout"].strip(),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "sandbox": "read-only",
        "marketplace": MARKETPLACE,
        "plugin": PLUGIN,
        "plugin_source_tree_sha256": source["tree_sha256"],
        "scenario_count": len(scenario_summaries),
        "all_passed": all(item["returncode"] == 0 for item in scenario_summaries),
        "raw_evidence_archive": archive_record,
        "scenarios": scenario_summaries,
    }
    write_json_atomic(evidence_dir / "summary.json", summary)
    readme = f"""# Clean Codex install and activation evidence

Generated `{summary['generated_at']}` with `{summary['codex_cli']}`, model
`{MODEL}`, reasoning effort `{REASONING_EFFORT}`, and a read-only sandbox.
The marketplace and plugin were installed into a newly created isolated Codex
home. The harness neither inspected, copied, nor persisted the existing Codex
auth file; Codex consumed it in place through a symlink. `HOME` pointed to a new
empty directory, and scientific-provider credentials/profiles were not inherited.

The installed plugin matched source tree SHA-256
`{source['tree_sha256']}` across {source['file_count']} files (cache/bytecode
excluded). The checksummed `{archive_record['path']}` contains each
exact prompt, raw Codex JSONL event trace, final answer,
command/config/exit record, timestamps, install evidence, and checksums. Its
SHA-256 is `{archive_record['sha256']}` ({archive_record['size_bytes']} bytes).
All {len(scenario_summaries)} deterministic scenarios passed, including the
adjacent negative prompt.
"""
    write_bytes_atomic(evidence_dir / "README.md", readme.encode("utf-8"))
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
