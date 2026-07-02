from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from clean_install_e2e import (
    parse_event_counts,
    safe_text,
    safe_environment,
    source_manifest,
    build_marketplace_snapshot,
    command_reads_installed_skill,
    SCENARIOS,
    contains_required_term,
    validated_evidence_members,
    verify_activation_trace,
    validate_resumed_run,
)


class CleanInstallEvidenceTests(unittest.TestCase):
    def test_source_manifest_includes_every_regular_plugin_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "code.txt").write_text("stable")
            (root / "evidence").mkdir()
            (root / "evidence" / "run.txt").write_text("changes")
            first = source_manifest(root)
            (root / "evidence" / "run.txt").write_text("changed again")
            second = source_manifest(root)
        self.assertNotEqual(first["tree_sha256"], second["tree_sha256"])
        self.assertEqual(
            [item["path"] for item in first["files"]],
            ["code.txt", "evidence/run.txt"],
        )

    def test_marketplace_snapshot_matches_plugin_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "marketplace"
            registry = build_marketplace_snapshot(root)
            parsed = json.loads(registry.read_text())
            snapshot = source_manifest(root / "plugins" / "biohub-esm")
        self.assertEqual(parsed["name"], "lsc110-local")
        self.assertEqual(
            snapshot["tree_sha256"],
            source_manifest(TESTS.parents[1] / "plugins" / "biohub-esm")[
                "tree_sha256"
            ],
        )

    def test_event_counts_requires_jsonl(self) -> None:
        trace = "\n".join(
            [json.dumps({"type": "thread.started"}), json.dumps({"type": "item.completed"})]
        )
        self.assertEqual(
            parse_event_counts(trace), {"item.completed": 1, "thread.started": 1}
        )
        with self.assertRaises(json.JSONDecodeError):
            parse_event_counts("not-json")

    def test_secret_pattern_refuses_persistence(self) -> None:
        with self.assertRaises(RuntimeError):
            safe_text("unexpected sk-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(
            safe_text("Authorization: Bearer abcdefghijklmnop"),
            "Authorization: Bearer [REDACTED]",
        )
        self.assertEqual(safe_text("ESM_API_KEY is configured or missing"), "ESM_API_KEY is configured or missing")
        self.assertTrue(contains_required_term("License: CC-BY-4.0", "CC BY 4.0"))

    def test_activation_requires_real_skill_file_access(self) -> None:
        installed_root = "/tmp/clean-codex/plugins/cache/biohub-esm/0.1.0"
        trace = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd-1",
                    "type": "command_execution",
                    "command": (
                        f"sed -n 1,200p {installed_root}/skills/biohub-esm/SKILL.md "
                        f"{installed_root}/skills/esmc/SKILL.md"
                    ),
                    "aggregated_output": "every /skills/esmfold2/SKILL.md listed here",
                },
            }
        )
        self.assertEqual(
            verify_activation_trace(
                trace, ("biohub-esm", "esmc"), [installed_root]
            ),
            [
                {
                    "skill": "biohub-esm",
                    "installed_root": installed_root,
                    "command_event_ids": ["cmd-1"],
                },
                {
                    "skill": "esmc",
                    "installed_root": installed_root,
                    "command_event_ids": ["cmd-1"],
                },
            ],
        )
        with self.assertRaises(RuntimeError):
            verify_activation_trace(
                trace, ("biohub-esm", "esmfold2"), [installed_root]
            )
        self.assertEqual(
            verify_activation_trace(
                '{"type":"thread.started"}', (), [installed_root]
            ),
            [],
        )
        with self.assertRaises(RuntimeError):
            verify_activation_trace(trace, (), [installed_root])
        with self.assertRaises(RuntimeError):
            verify_activation_trace(
                trace, ("biohub-esm", "esmc"), ["/different/install"]
            )
        variable_command = (
            f"plugin={installed_root}; sed -n '1,200p' "
            '"$plugin/skills/esm-atlas/SKILL.md"'
        )
        self.assertTrue(
            command_reads_installed_skill(
                variable_command, root=installed_root, skill="esm-atlas"
            )
        )

    def test_clean_environment_rewrites_home_and_excludes_provider_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = safe_environment(root / "codex", root / "home")
        self.assertEqual(env["HOME"], str(root / "home"))
        self.assertEqual(env["CODEX_HOME"], str(root / "codex"))
        for name in (
            "ESM_API_KEY",
            "HF_TOKEN",
            "MODAL_TOKEN_ID",
            "MODAL_TOKEN_SECRET",
        ):
            self.assertNotIn(name, env)

    def test_resume_recomputes_evidence_hashes_and_source_identity(self) -> None:
        prompt = "prompt\n"
        trace = '{"type":"turn.completed"}\n'
        final = "answer"
        digest = "a" * 64
        record = {
            "returncode": 0,
            "plugin_source_tree_sha256": digest,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "events_sha256": hashlib.sha256(trace.encode()).hexdigest(),
            "final_sha256": hashlib.sha256(final.encode()).hexdigest(),
        }
        validate_resumed_run(
            record,
            scenario_id="example",
            prompt=prompt,
            trace=trace,
            final=final,
            source_digest=digest,
        )
        for field, value in (
            ("events_sha256", "0" * 64),
            ("plugin_source_tree_sha256", "b" * 64),
            ("returncode", 1),
        ):
            tampered = dict(record)
            tampered[field] = value
            with self.assertRaises(RuntimeError):
                validate_resumed_run(
                    tampered,
                    scenario_id="example",
                    prompt=prompt,
                    trace=trace,
                    final=final,
                    source_digest=digest,
                )

    def test_compaction_rejects_unallowlisted_scenario_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("install.json", "source-tree.json"):
                (root / name).write_text("{}")
            for scenario in SCENARIOS:
                scenario_dir = root / scenario["id"]
                scenario_dir.mkdir()
                for name in ("prompt.txt", "events.jsonl", "final.txt", "run.json"):
                    (scenario_dir / name).write_text("{}")
            self.assertEqual(len(validated_evidence_members(root)), 22)
            (root / SCENARIOS[0]["id"] / "secret.txt").write_text("must not archive")
            with self.assertRaisesRegex(RuntimeError, "artifact allowlist"):
                validated_evidence_members(root)


if __name__ == "__main__":
    unittest.main()
