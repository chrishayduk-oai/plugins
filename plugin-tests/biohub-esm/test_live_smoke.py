from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from live_smoke import (
    cleanup_modal_volumes,
    managed_provider_call,
    parse_modal_binder_result,
    run_modal_binder,
    run_modal_command_with_cleanup,
    run_modal_fold,
    save_result,
    validate_modal_fold_result,
)
from biohub_esm_lib.constants import (
    ESM_GIT_REVISION,
    HF_REVISIONS,
    TRANSFORMERS_GIT_REVISION,
)
from biohub_esm_lib.provenance import input_digest
from biohub_esm_lib.errors import ValidationError


class LiveSmokeParsingTests(unittest.TestCase):
    @staticmethod
    def valid_modal_fold_result() -> dict:
        return {
            "mean_plddt": 0.8,
            "ptm": 0.7,
            "iptm": None,
            "model_id": "biohub/ESMFold2-Fast",
            "model_revision": HF_REVISIONS["biohub/ESMFold2-Fast"],
            "esm_git_revision": ESM_GIT_REVISION,
            "transformers_git_revision": TRANSFORMERS_GIT_REVISION,
            "seed": 0,
            "input_sha256": input_digest("MKTAYIAKQRQISFVKSHFSRQ"),
        }

    def test_modal_fold_result_is_bound_to_remote_pins_and_input(self) -> None:
        self.assertEqual(
            validate_modal_fold_result(self.valid_modal_fold_result())["seed"], 0
        )
        for field, value in (
            ("model_revision", "0" * 40),
            ("esm_git_revision", "0" * 40),
            ("transformers_git_revision", "0" * 40),
            ("input_sha256", "0" * 64),
            ("seed", 1),
            ("mean_plddt", float("nan")),
        ):
            with self.subTest(field=field):
                result = self.valid_modal_fold_result()
                result[field] = value
                with self.assertRaises(ValidationError):
                    validate_modal_fold_result(result)

    def test_modal_fold_rejects_stale_output_before_provider_call(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "modal-result.json").write_text("{}")
            with patch("live_smoke.run_modal_command_with_cleanup") as run:
                with self.assertRaisesRegex(ValidationError, "stale evidence"):
                    run_modal_fold(
                        argparse.Namespace(
                            confirm_cost=True,
                            max_gpu_cost_usd=1.3164,
                            confirm_volume_cleanup=True,
                            output_dir=str(root),
                            modal_cli="/modal",
                        )
                    )
            run.assert_not_called()
    def test_managed_provider_call_records_exact_endpoint_and_digests(self) -> None:
        call = managed_provider_call(
            endpoint="https://biohub.ai/api/v1/encode",
            operation="encode",
            inputs="MKT",
            parameters={"model": "esmc-300m-2024-12"},
            started_at="2026-07-01T00:00:00Z",
            finished_at="2026-07-01T00:00:01Z",
        )
        self.assertEqual(call["endpoint"], "https://biohub.ai/api/v1/encode")
        self.assertEqual(call["operation"], "encode")
        self.assertEqual(len(call["input_sha256"]), 64)
        self.assertEqual(len(call["parameters_sha256"]), 64)
        self.assertEqual(call["parameters"], {"model": "esmc-300m-2024-12"})

    def test_managed_fold_result_digest_binds_provenance_input(self) -> None:
        inputs = {
            "sequences": [
                {"type": "protein", "id": "A", "sequence": "MKT"}
            ]
        }
        digest = input_digest(inputs)
        with tempfile.TemporaryDirectory() as directory:
            saved = save_result(
                Path(directory),
                result={"input_sha256": digest, "sequence_sha256": input_digest("MKT")},
                route="biohub",
                endpoint="https://biohub.ai/api/v1/fold_all_atom",
                model_id="esmfold2-fast-2026-05",
                model_revision="esmfold2-fast-2026-05",
                inputs=inputs,
                parameters={"num_loops": 3},
                seed=None,
                started_at="2026-07-01T00:00:00Z",
                provider_calls=[
                    managed_provider_call(
                        endpoint="https://biohub.ai/api/v1/fold_all_atom",
                        operation="fold_all_atom",
                        inputs=inputs,
                        parameters={"model": "esmfold2-fast-2026-05", "num_loops": 3},
                        started_at="2026-07-01T00:00:00Z",
                        finished_at="2026-07-01T00:00:01Z",
                    )
                ],
            )
            result = json.loads(Path(saved["result"]).read_text())
            provenance = json.loads(Path(saved["provenance"]).read_text())
        self.assertEqual(result["input_sha256"], provenance["input_sha256"])
        self.assertEqual(provenance["provider_calls"][0]["input_sha256"], digest)

    def test_parse_modal_binder_summary(self) -> None:
        value = {
            "sequences": ["MKTAYIAK|MKTLALV"],
            "trajectory": {"0": {"loss": [1.0]}, "1": {"loss": [0.5]}},
            "critic_results": [{"final_loss": -1.25, "complex": "ATOM"}],
        }
        result = parse_modal_binder_result(value)
        self.assertEqual(result["generated_sequences"], ["MKTAYIAK|MKTLALV"])
        self.assertEqual(result["trajectory_steps"], 2)
        self.assertEqual(result["average_final_loss"], -1.25)
        self.assertEqual(len(result["generated_sequences_sha256"]), 64)
        self.assertEqual(len(result["component_digests"][0]["binder_sha256"]), 64)

    def test_missing_modal_summary_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_modal_binder_result("container exited without a result")

    def test_modal_binder_rejects_nonfinite_losses_and_invalid_residues(self) -> None:
        for value in (
            {
                "sequences": ["MKT|MRA"],
                "trajectory": {"0": {"loss": [float("nan")]}},
                "critic_results": [{"final_loss": 1.0}],
            },
            {
                "sequences": ["MKT|MRA"],
                "trajectory": {"0": {"loss": [1.0]}},
                "critic_results": [{"final_loss": float("inf")}],
            },
            {
                "sequences": ["MKT|MJZ"],
                "trajectory": {"0": {"loss": [1.0]}},
                "critic_results": [{"final_loss": 1.0}],
            },
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_modal_binder_result(value)

    def test_modal_fold_requires_visible_confirmed_cost(self) -> None:
        with self.assertRaisesRegex(ValidationError, "confirm-cost"):
            run_modal_fold(
                argparse.Namespace(confirm_cost=False, max_gpu_cost_usd=1.3164)
            )
        with self.assertRaisesRegex(ValidationError, "upper-bound"):
            run_modal_fold(
                argparse.Namespace(confirm_cost=True, max_gpu_cost_usd=1.0)
            )
        for value in (float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValidationError, "upper-bound"
            ):
                run_modal_fold(
                    argparse.Namespace(confirm_cost=True, max_gpu_cost_usd=value)
                )
        with self.assertRaisesRegex(ValidationError, "volume-cleanup"):
            run_modal_fold(
                argparse.Namespace(
                    confirm_cost=True,
                    max_gpu_cost_usd=1.3164,
                    confirm_volume_cleanup=False,
                )
            )

    def test_modal_binder_rejects_nonfinite_cost_ceiling(self) -> None:
        for value in (float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValidationError, "upper-bound"
            ):
                run_modal_binder(
                    argparse.Namespace(confirm_cost=True, max_gpu_cost_usd=value)
                )

    def test_modal_volume_cleanup_is_ticket_scoped_and_noninteractive(self) -> None:
        completed = __import__("subprocess").CompletedProcess([], 0, "deleted", "")
        with patch("live_smoke.subprocess.run", return_value=completed) as run:
            result = cleanup_modal_volumes("/modal", ("lsc110-test-volume",))
        self.assertTrue(result["all_succeeded"])
        self.assertEqual(
            run.call_args.args[0],
            [
                "/modal",
                "volume",
                "delete",
                "lsc110-test-volume",
                "--allow-missing",
                "--yes",
            ],
        )

    def test_modal_cleanup_attempts_every_volume_after_delete_failure(self) -> None:
        completed = __import__("subprocess").CompletedProcess([], 0, "deleted", "")
        with patch(
            "live_smoke.subprocess.run",
            side_effect=[OSError("first delete failed"), completed],
        ) as run:
            result = cleanup_modal_volumes("/modal", ("lsc110-a", "lsc110-b"))
        self.assertFalse(result["all_succeeded"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual([item["volume"] for item in result["results"]], ["lsc110-a", "lsc110-b"])

    def test_modal_cleanup_runs_on_interrupt(self) -> None:
        completed = __import__("subprocess").CompletedProcess([], 0, "deleted", "")
        with patch(
            "live_smoke.subprocess.run",
            side_effect=[KeyboardInterrupt(), completed],
        ) as run:
            with self.assertRaises(KeyboardInterrupt):
                run_modal_command_with_cleanup(
                    ["/modal", "run"],
                    modal_cli="/modal",
                    volume_names=("lsc110-volume",),
                    timeout=1,
                )
        self.assertEqual(run.call_count, 2)

    def test_modal_fold_confidence_scalars_are_json_serializable(self) -> None:
        source = (TESTS / "modal_esmfold2_smoke.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_optional_float"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(ast.Module([helper], type_ignores=[]), "<helper>", "exec"),
            namespace,
        )
        convert = namespace["_optional_float"]
        payload = {"ptm": convert(0.75), "iptm": convert(None)}
        self.assertEqual(
            json.loads(json.dumps(payload)), {"ptm": 0.75, "iptm": None}
        )

    def test_modal_fold_replaces_mutable_transformers_and_runtime_verifies(self) -> None:
        source = (TESTS / "modal_esmfold2_smoke.py").read_text(encoding="utf-8")
        self.assertIn('extra_options="--force-reinstall --no-deps"', source)
        self.assertIn('"requested_revision"', source)
        self.assertIn("_verified_vcs_revision(", source)
        self.assertIn('"transformers", TRANSFORMERS_REVISION', source)


if __name__ == "__main__":
    unittest.main()
