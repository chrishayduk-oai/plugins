from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm"
CLI = ROOT / "scripts" / "biohub_esm.py"
sys.path.insert(0, str(ROOT / "scripts"))

from biohub_esm import (
    _persist_schema_drift,
    _save_atlas_artifact_provenance,
    command_atlas_batch_cancel,
    command_atlas_batch_status,
    command_atlas_batch_submit,
    command_atlas_batch_wait,
    command_atlas_cluster,
    command_atlas_protein,
    command_atlas_search,
    command_atlas_thumbnail,
)
from biohub_esm_lib.atlas_jobs import AtlasBatchStore
from biohub_esm_lib.errors import SchemaDriftError
from biohub_esm_lib.provenance import validate_provenance


class CLITests(unittest.TestCase):
    def run_cli(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        values = {"PATH": os.environ.get("PATH", ""), "PYTHONSAFEPATH": "1"}
        if env:
            values.update(env)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            text=True,
            capture_output=True,
            check=False,
            env=values,
        )

    def test_help_and_preflight(self) -> None:
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        result = self.run_cli("preflight", env={"ESM_API_KEY": "secret-value"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["biohub_managed"]["status"], "configured")
        self.assertNotIn("secret-value", result.stdout + result.stderr)

    def test_atlas_cli_does_not_expose_a_host_override(self) -> None:
        result = self.run_cli(
            "atlas",
            "--base-url",
            "https://example.test",
            "search",
            "--sequence",
            "MKT",
        )
        self.assertEqual(result.returncode, 2)
        help_result = self.run_cli("atlas", "--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertNotIn("--base-url", help_result.stdout)

    def test_router_json(self) -> None:
        result = self.run_cli("route", "--task", "binder-design")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["route"], "modal")
        self.assertIn("1,000", " ".join(payload["warnings"]))

    def test_invalid_input_has_structured_error(self) -> None:
        result = self.run_cli(
            "validate-sequence", "--target", "esmc", "--sequence", "BAD*SEQUENCE"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stderr))

    def test_validate_fold_from_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "input.json"
            config = Path(directory) / "config.json"
            payload.write_text(json.dumps({"sequences": [{"type": "protein", "id": "A", "sequence": "MKT"}]}))
            config.write_text(json.dumps({"num_loops": 3, "num_sampling_steps": 50}))
            result = self.run_cli(
                "validate-fold",
                "--model",
                "esmfold2-fast-2026-05",
                "--input",
                str(payload),
                "--config",
                str(config),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_schema_drift_diagnostic_is_redacted_and_not_embedded_in_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            error = SchemaDriftError(
                "schema changed",
                raw={"authorization": "Bearer secret-value", "new_field": 1},
            )
            args = SimpleNamespace(output_dir=directory, command="atlas")
            _persist_schema_drift(args, error)
            diagnostic = Path(error.diagnostic_path or "")
            self.assertEqual(
                json.loads(diagnostic.read_text()),
                {"authorization": "[REDACTED]", "new_field": 1},
            )
            self.assertNotIn("secret-value", json.dumps(error.as_dict()))

    def test_atlas_binary_artifact_gets_complete_provenance_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "batch.zip"
            artifact.write_bytes(b"PK-test")
            result = _save_atlas_artifact_provenance(
                artifact,
                media_type="application/zip",
                endpoint_path="/proteins/batch",
                inputs={"protein_hashes": ["a" * 32]},
                parameters={"topk_features": 10},
                started_at="2026-07-01T00:00:00Z",
            )
            provenance = json.loads(Path(result["provenance"]).read_text())
            validate_provenance(provenance)
            self.assertEqual(provenance["execution_route"], "atlas-api")
            self.assertEqual(provenance["artifacts"][0]["sha256"], result["artifact"]["sha256"])

    def test_atlas_batch_cli_persists_submit_status_wait_and_download(self) -> None:
        class FakeAtlas:
            def __init__(self) -> None:
                self.statuses = [
                    (202, {"job_id": "job-1", "status": "pending", "completed_count": 1, "total_count": 2}),
                    (
                        200,
                        {
                            "job_id": "job-1",
                            "status": "completed",
                            "completed_count": 2,
                            "total_count": 2,
                            "download_url": "https://download.test/result.zip?X-Amz-Signature=secret-value",
                        },
                    ),
                ]

            def submit_batch(self, hashes, **parameters):
                self.hashes = hashes
                self.parameters = parameters
                return 202, {"job_id": "job-1", "status": "pending"}, {}

            def batch_status(self, job_id):
                self.job_id = job_id
                return self.statuses.pop(0)

            def download(self, url, destination):
                self.download_url = url
                destination.write_bytes(b"PK-test")
                return {
                    "path": str(destination),
                    "size_bytes": destination.stat().st_size,
                    "sha256": "0" * 64,
                    "resumed": False,
                    "http_status": 206,
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hashes = root / "hashes.json"
            hashes.write_text(json.dumps(["A" * 32, "a" * 32, "b" * 32]))
            state_path = root / "batch-state.json"
            output_path = root / "batch.zip"
            common = {
                "base_url": "https://biohub.ai",
                "timeout": 1,
                "state": str(state_path),
                "job_id": None,
            }
            fake = FakeAtlas()
            with patch("biohub_esm.AtlasClient", return_value=fake), redirect_stdout(StringIO()):
                command_atlas_batch_submit(
                    SimpleNamespace(
                        **common,
                        hashes=str(hashes),
                        topk_features=10,
                        no_structure=False,
                        no_cluster_info=False,
                        no_sequence=False,
                        no_features=False,
                        no_per_residue_features=False,
                        output=str(output_path),
                    )
                )
                command_atlas_batch_status(SimpleNamespace(**common))
                command_atlas_batch_wait(
                    SimpleNamespace(
                        **common,
                        poll_interval=0.001,
                        poll_timeout=1,
                        output=str(output_path),
                    )
                )
            state = AtlasBatchStore(state_path).load()
            self.assertEqual(fake.hashes, ["a" * 32, "b" * 32])
            from biohub_esm_lib.provenance import input_digest

            self.assertEqual(
                state["request"]["input_sha256"],
                input_digest(["a" * 32, "b" * 32]),
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(
                [call["operation"] for call in state["provider_calls"]],
                ["submit", "status", "status", "download"],
            )
            self.assertEqual(state["provider_calls"][-1]["http_status"], 206)
            self.assertEqual(
                state["last_response"]["download_url"],
                "[EPHEMERAL URL OMITTED]",
            )
            persisted = state_path.read_text() + AtlasBatchStore(state_path).provenance_path.read_text()
            self.assertNotIn("secret-value", persisted)
            validate_provenance(
                json.loads(AtlasBatchStore(state_path).provenance_path.read_text())
            )
            self.assertTrue(output_path.read_bytes().startswith(b"PK"))

    def test_atlas_batch_cli_can_adopt_and_cancel_existing_job(self) -> None:
        class FakeAtlas:
            def cancel_batch(self, job_id):
                self.job_id = job_id

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "adopted-state.json"
            args = SimpleNamespace(
                base_url="https://biohub.ai",
                timeout=1,
                state=str(state_path),
                job_id="job-adopted",
            )
            fake = FakeAtlas()
            with patch("biohub_esm.AtlasClient", return_value=fake), redirect_stdout(StringIO()):
                command_atlas_batch_cancel(args)
            state = AtlasBatchStore(state_path).load()
            self.assertEqual(fake.job_id, "job-adopted")
            self.assertEqual(state["status"], "cancellation-requested")
            self.assertEqual(
                [call["operation"] for call in state["provider_calls"]],
                ["adopt", "cancel"],
            )

    def test_atlas_batch_cancel_preserves_terminal_state(self) -> None:
        class FakeAtlas:
            def cancel_batch(self, job_id):
                raise AssertionError("terminal state must not call cancel")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "terminal-state.json"
            store = AtlasBatchStore(state_path)
            state = store.adopt(
                job_id="job-complete",
                endpoint="https://biohub.ai/esm/protein/api/v1alpha1/proteins/batch/jobs/job-complete",
            )
            store.update(
                response={"job_id": "job-complete", "status": "completed"},
                http_status=200,
                provider_call={
                    "endpoint": "https://biohub.ai/esm/protein/api/v1alpha1/proteins/batch/jobs/job-complete",
                    "operation": "status",
                },
            )
            args = SimpleNamespace(
                base_url="https://biohub.ai",
                timeout=1,
                state=str(state_path),
                job_id=None,
            )
            with patch("biohub_esm.AtlasClient", return_value=FakeAtlas()), redirect_stdout(StringIO()):
                command_atlas_batch_cancel(args)
            after = store.load()
            self.assertEqual(after["status"], "completed")
            self.assertEqual(
                [call["operation"] for call in after["provider_calls"]],
                ["adopt", "status"],
            )

    def test_atlas_batch_pending_poll_preserves_cancellation_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtlasBatchStore(Path(directory) / "cancel-race.json")
            store.adopt(
                job_id="job-race",
                endpoint="https://biohub.ai/esm/protein/api/v1alpha1/proteins/batch/jobs/job-race",
            )
            store.update(
                response={"job_id": "job-race", "status": "cancellation-requested"},
                http_status=204,
                status="cancellation-requested",
                provider_call={"endpoint": "x", "operation": "cancel"},
            )
            pending = store.update(
                response={"job_id": "job-race", "status": "pending"},
                http_status=202,
                provider_call={"endpoint": "x", "operation": "status"},
            )
            self.assertEqual(pending["status"], "cancellation-requested")
            self.assertEqual(pending["last_response"]["status"], "pending")
            completed = store.update(
                response={"job_id": "job-race", "status": "completed"},
                http_status=200,
                provider_call={"endpoint": "x", "operation": "status"},
            )
            self.assertEqual(completed["status"], "completed")

    def test_atlas_cli_normalizes_sequences_and_md5_before_provenance(self) -> None:
        class FakeAtlas:
            def search(self, sequence, **parameters):
                self.search_sequence = sequence
                return {"query_sequence": sequence, "similar_proteins": []}

            def protein(self, protein_hash, **parameters):
                self.protein_hash = protein_hash
                self.fold_sequence = parameters["sequence_for_fold"]
                return {"protein_hash": protein_hash}

            def cluster(self, protein_hash, **parameters):
                self.cluster_hash = protein_hash
                return {
                    "protein_hash": protein_hash,
                    "cluster_size": 1,
                    "member_protein_hashes": [protein_hash],
                }

            def thumbnail(self, protein_hash, thumbnail_type):
                self.thumbnail_hash = protein_hash
                return SimpleNamespace(body=b"\x89PNG\r\n\x1a\nmock")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digest = __import__("hashlib").md5(
                b"MKT", usedforsecurity=False
            ).hexdigest()
            fake = FakeAtlas()
            common = {"base_url": "https://biohub.ai", "timeout": 1}
            with patch("biohub_esm.AtlasClient", return_value=fake), redirect_stdout(StringIO()):
                command_atlas_search(
                    SimpleNamespace(
                        **common,
                        sequence=">query\nmk t\n",
                        topk_results=2,
                        topk_features=3,
                        min_similarity=0.5,
                        cluster_pct_characterized_max=None,
                        include_cluster_info=False,
                        output_dir=str(root / "search"),
                    )
                )
                command_atlas_protein(
                    SimpleNamespace(
                        **common,
                        protein_hash=digest.upper(),
                        topk_features=3,
                        fold_on_miss=True,
                        raw_features=False,
                        feature_index=None,
                        sequence=">query\nmkt\n",
                        sequence_file=None,
                        output_dir=str(root / "protein"),
                    )
                )
                command_atlas_cluster(
                    SimpleNamespace(
                        **common,
                        protein_hash=digest.upper(),
                        topk_features=3,
                        output_dir=str(root / "cluster"),
                    )
                )
                command_atlas_thumbnail(
                    SimpleNamespace(
                        **common,
                        protein_hash=digest.upper(),
                        thumbnail_type="plddt",
                        output=str(root / "thumbnail.png"),
                    )
                )
            self.assertEqual(fake.search_sequence, "MKT")
            self.assertEqual(fake.fold_sequence, "MKT")
            self.assertEqual(fake.protein_hash, digest)
            self.assertEqual(fake.cluster_hash, digest)
            self.assertEqual(fake.thumbnail_hash, digest)
            search_provenance = json.loads((root / "search" / "provenance.json").read_text())
            protein_provenance = json.loads((root / "protein" / "provenance.json").read_text())
            self.assertEqual(
                search_provenance["input_sha256"],
                __import__("hashlib").sha256(b"MKT").hexdigest(),
            )
            self.assertIn(digest, protein_provenance["endpoint"])
            self.assertNotIn(digest.upper(), protein_provenance["endpoint"])


if __name__ == "__main__":
    unittest.main()
