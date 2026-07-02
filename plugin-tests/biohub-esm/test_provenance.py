from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.constants import (
    ESM_GIT_REVISION,
    HF_REVISIONS,
    MODAL_BINDER_HF_REVISIONS,
    TRANSFORMERS_GIT_REVISION,
)
from biohub_esm_lib.errors import ValidationError
from biohub_esm_lib.provenance import (
    artifact_record,
    build_provenance,
    materialize_pdb_fields,
    validate_provenance,
    verify_installed_vcs_revision,
    write_json_atomic,
)


class ProvenanceTests(unittest.TestCase):
    def test_complete_provenance_and_hf_pin_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.cif"
            artifact.write_text("data_test\n", encoding="utf-8")
            record = artifact_record(artifact, media_type="chemical/x-mmcif")
            value = build_provenance(
                route="self-hosted",
                endpoint="huggingface://biohub/ESMFold2",
                model_id="biohub/ESMFold2",
                model_revision=None,
                inputs={"sequence": "MKT"},
                parameters={"num_loops": 3},
                seed=0,
                started_at="2026-07-01T00:00:00Z",
                artifacts=[record],
                confidence_metrics={"ptm": 0.8},
            )
        self.assertEqual(value["model_revision"], HF_REVISIONS["biohub/ESMFold2"])
        self.assertEqual(value["model_revision_kind"], "hugging-face-commit")
        self.assertIsNone(value["esm_git_revision"])
        self.assertIsNone(value["transformers_git_revision"])
        self.assertEqual(len(value["input_sha256"]), 64)

    def test_explicit_code_pins_and_installed_vcs_verification(self) -> None:
        distribution = Mock()
        distribution.read_text.return_value = json.dumps(
            {
                "vcs_info": {
                    "commit_id": ESM_GIT_REVISION,
                    "requested_revision": ESM_GIT_REVISION,
                }
            }
        )
        with patch("biohub_esm_lib.provenance.metadata.distribution", return_value=distribution):
            self.assertEqual(
                verify_installed_vcs_revision("esm", ESM_GIT_REVISION),
                ESM_GIT_REVISION,
            )
        value = build_provenance(
            route="self-hosted",
            endpoint="local://esm",
            model_id="biohub/ESMC-300M",
            model_revision=None,
            inputs="MKT",
            parameters={},
            seed=0,
            started_at="2026-07-01T00:00:00Z",
            artifacts=[],
            esm_git_revision=ESM_GIT_REVISION,
            transformers_git_revision=TRANSFORMERS_GIT_REVISION,
        )
        self.assertEqual(value["esm_git_revision"], ESM_GIT_REVISION)
        self.assertEqual(value["transformers_git_revision"], TRANSFORMERS_GIT_REVISION)

    def test_explicit_input_digest_avoids_persisting_or_rehashing_input(self) -> None:
        digest = "a" * 64
        value = build_provenance(
            route="biohub",
            endpoint="https://biohub.ai/api/v1/fold_all_atom",
            model_id="esmfold2-fast-2026-05",
            model_revision="managed:esmfold2-fast-2026-05",
            inputs=None,
            input_sha256=digest,
            parameters={},
            seed=None,
            started_at="2026-07-01T00:00:00Z",
            artifacts=[],
        )
        self.assertEqual(value["input_sha256"], digest)

    def test_parameters_are_redacted_before_persistence(self) -> None:
        value = build_provenance(
            route="biohub",
            endpoint="https://biohub.ai/api/v1/logits",
            model_id="esmc-300m-2024-12",
            model_revision="managed:esmc-300m-2024-12",
            inputs="MKT",
            parameters={"nested": {"api_key": "must-not-persist"}},
            seed=None,
            started_at="2026-07-01T00:00:00Z",
            artifacts=[],
        )
        self.assertNotIn("must-not-persist", repr(value))

    def test_hf_model_revision_cannot_drift_from_validated_pin(self) -> None:
        with self.assertRaisesRegex(ValidationError, "validated pin"):
            build_provenance(
                route="self-hosted",
                endpoint="huggingface://biohub/ESMFold2",
                model_id="biohub/ESMFold2",
                model_revision="0" * 40,
                inputs="MKT",
                parameters={},
                seed=0,
                started_at="2026-07-01T00:00:00Z",
                artifacts=[],
            )

    def test_experimental_binder_revision_cannot_drift(self) -> None:
        model_id = "biohub/ESMFold2-Experimental-Fast"
        self.assertIn(model_id, MODAL_BINDER_HF_REVISIONS)
        with self.assertRaisesRegex(ValidationError, "validated pin"):
            build_provenance(
                route="modal",
                endpoint="modal://binder/sweep",
                model_id=model_id,
                model_revision="0" * 40,
                inputs="MKT",
                parameters={"seed": 0},
                seed=0,
                started_at="2026-07-01T00:00:00Z",
                artifacts=[],
            )

    def test_atomic_json_is_valid_and_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json_atomic(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_nonfinite_values_are_rejected_before_digest_or_persistence(self) -> None:
        from biohub_esm_lib.provenance import input_digest

        with self.assertRaisesRegex(ValidationError, "non-finite"):
            input_digest({"score": float("nan")})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            with self.assertRaisesRegex(ValidationError, "non-finite"):
                write_json_atomic(path, {"score": float("inf")})
            self.assertFalse(path.exists())

    def test_materializes_embedded_pdb_with_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, artifacts = materialize_pdb_fields(
                {"similar_proteins": [{"protein_hash": "a" * 32, "pdb": "ATOM\n"}]},
                Path(directory),
                prefix="hit",
            )
            path = Path(artifacts[0]["path"])
            self.assertEqual(path.read_text(), "ATOM\n")
            self.assertIn("pdb_artifact", normalized["similar_proteins"][0])
            self.assertEqual(len(artifacts[0]["sha256"]), 64)

    def test_missing_or_bad_checksum_rejected(self) -> None:
        value = {
            "schema_version": "1.0",
            "execution_route": "biohub",
            "endpoint": "x",
            "provider_calls": [{"endpoint": "x"}],
            "model_id": "m",
            "model_revision": "r",
            "model_revision_kind": "explicit",
            "esm_git_revision": None,
            "transformers_git_revision": None,
            "input_sha256": "0" * 64,
            "parameters": {},
            "seed": 0,
            "started_at": "2026-07-01T00:00:00Z",
            "finished_at": "2026-07-01T00:00:01Z",
            "artifacts": [
                {
                    "path": "x",
                    "size_bytes": 1,
                    "media_type": "application/json",
                    "sha256": "bad",
                }
            ],
            "confidence_metrics": {},
        }
        with self.assertRaises(ValidationError):
            validate_provenance(value)

    def test_provenance_rejects_incomplete_artifacts_and_bad_time_order(self) -> None:
        valid = build_provenance(
            route="biohub+atlas-api",
            endpoint="https://biohub.ai",
            model_id=None,
            model_revision=None,
            inputs="MKT",
            parameters={},
            seed=None,
            started_at="2026-07-01T00:00:00Z",
            finished_at="2026-07-01T00:00:01Z",
            artifacts=[],
            provider_calls=[
                {
                    "endpoint": "https://biohub.ai/api/v1/encode",
                    "method": "POST",
                    "started_at": "2026-07-01T00:00:00Z",
                    "finished_at": "2026-07-01T00:00:01Z",
                }
            ],
        )
        cases = []
        for artifact in (
            {"size_bytes": 1, "media_type": "text/plain", "sha256": "0" * 64},
            {"path": "x", "media_type": "text/plain", "sha256": "0" * 64},
            {"path": "x", "size_bytes": -1, "media_type": "text/plain", "sha256": "0" * 64},
            {"path": "x", "size_bytes": 1, "sha256": "0" * 64},
        ):
            candidate = dict(valid)
            candidate["artifacts"] = [artifact]
            cases.append(candidate)
        reversed_time = dict(valid)
        reversed_time["finished_at"] = "2025-07-01T00:00:00Z"
        cases.append(reversed_time)
        bad_call = dict(valid)
        bad_call["provider_calls"] = [{"endpoint": "x", "method": "FETCH"}]
        cases.append(bad_call)
        bad_route = dict(valid)
        bad_route["execution_route"] = "biohub+unknown"
        cases.append(bad_route)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                validate_provenance(candidate)

    def test_provenance_rejects_incoherent_schema_seed_and_model_fields(self) -> None:
        valid = build_provenance(
            route="biohub",
            endpoint="https://biohub.ai/api/v1/logits",
            model_id="esmc-300m-2024-12",
            model_revision="esmc-300m-2024-12",
            inputs="MKT",
            parameters={},
            seed=0,
            started_at="2026-07-01T00:00:00Z",
            finished_at="2026-07-01T00:00:01Z",
            artifacts=[],
        )
        mutations = (
            {"schema_version": "999"},
            {"seed": "zero"},
            {"seed": True},
            {
                "model_id": None,
                "model_revision": "invented",
                "model_revision_kind": "hugging-face-commit",
            },
            {
                "model_id": "esmc-300m-2024-12",
                "model_revision": "wrong",
                "model_revision_kind": "versioned-managed-model-id",
            },
            {
                "model_id": None,
                "model_revision": None,
                "model_revision_kind": "explicit",
            },
        )
        for mutation in mutations:
            candidate = {**valid, **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                validate_provenance(candidate)


if __name__ == "__main__":
    unittest.main()
