from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.errors import ValidationError
from biohub_esm_lib.modal_jobs import (
    CancelledCallError,
    ModalJobManager,
    ModalJobStore,
)
from biohub_esm_lib.constants import (
    ESM_GIT_REVISION,
    HF_REVISIONS,
    TRANSFORMERS_GIT_REVISION,
)
from biohub_esm_lib.provenance import build_provenance, input_digest
from biohub_esm_lib.provenance import write_json_atomic


def modal_result(result: object, payload: dict) -> dict:
    endpoint = "modal://test-app/test-function"
    provenance = build_provenance(
        route="modal",
        endpoint=endpoint,
        model_id="biohub/ESMFold2-Fast",
        model_revision=HF_REVISIONS["biohub/ESMFold2-Fast"],
        inputs=payload,
        parameters={"seed": 0},
        seed=0,
        started_at="2026-07-01T00:00:00Z",
        finished_at="2026-07-01T00:00:01Z",
        artifacts=[
            {
                "path": "/remote/result.json",
                "size_bytes": 2,
                "media_type": "application/json",
                "sha256": "0" * 64,
            }
        ],
        provider_calls=[{"endpoint": endpoint, "operation": "fold"}],
        esm_git_revision=ESM_GIT_REVISION,
        transformers_git_revision=TRANSFORMERS_GIT_REVISION,
    )
    return {
        "submission_sha256": input_digest(payload),
        "result": result,
        "provenance": provenance,
    }


class FakeModal:
    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.cancelled: list[str] = []
        self.results: list[object] = []
        self.spawn_failure_at: int | None = None
        self.gather_error: Exception | None = None

    def spawn(self, payload):
        index = len(self.spawned)
        self.spawned.append(payload)
        if self.spawn_failure_at == index:
            raise RuntimeError("spawn failed with token_secret=should-redact")
        return f"fc-{index}"

    def provider_identity(self):
        return {"app_name": "test-app", "function_name": "test-function"}

    def gather(self, call_ids, *, timeout):
        if self.gather_error:
            raise self.gather_error
        return [self.results[int(call_id.rsplit("-", 1)[1])] for call_id in call_ids]

    def cancel(self, call_id):
        self.cancelled.append(call_id)


class ModalJobTests(unittest.TestCase):
    def manager(self, directory: str, adapter: FakeModal) -> ModalJobManager:
        return ModalJobManager(adapter, ModalJobStore(Path(directory) / "jobs.json"))

    def test_spawn_persists_each_call_and_gather_completes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            state = manager.spawn([{"seed": 0}, {"seed": 1}], kind="fold")
            self.assertEqual([job["call_id"] for job in state["jobs"]], ["fc-0", "fc-1"])
            self.assertTrue((Path(directory) / "jobs.json").is_file())
            adapter.results = [
                modal_result({"ok": 0}, {"seed": 0}),
                modal_result({"ok": 1}, {"seed": 1}),
            ]
            result = manager.gather()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["jobs"][1]["result"]["result"], {"ok": 1})
        self.assertEqual(result["scope"], "control-plane-only")

    def test_spawn_failure_is_partial_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            adapter.spawn_failure_at = 1
            state = self.manager(directory, adapter).spawn(
                [{"seed": 0}, {"seed": 1}], kind="binder-design"
            )
        self.assertEqual(state["jobs"][0]["status"], "pending")
        self.assertEqual(state["jobs"][1]["status"], "submission-indeterminate")
        self.assertEqual(state["status"], "submission-indeterminate")
        self.assertTrue(
            state["jobs"][1]["manual_reconciliation"]["do_not_resubmit"]
        )
        self.assertNotIn("should-redact", state["jobs"][1]["spawn_error"])

    def test_gather_partial_exception_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}, {"seed": 1}], kind="fold")
            adapter.results = [
                modal_result({"ok": True}, {"seed": 0}),
                RuntimeError("remote failure"),
            ]
            state = manager.gather()
        self.assertEqual(state["status"], "partial")
        self.assertEqual([job["status"] for job in state["jobs"]], ["completed", "failed"])

    def test_non_json_result_fails_without_persisting_repr(self) -> None:
        class SecretBearingObject:
            def __repr__(self) -> str:
                return "ESM_API_KEY=must-not-persist"

        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            adapter.results = [SecretBearingObject()]
            state = manager.gather()
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("must-not-persist", repr(state))

    def test_gather_transport_failure_remains_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            adapter.gather_error = RuntimeError("temporary")
            state = manager.gather()
            reloaded = manager.store.load()
        self.assertEqual(state["status"], "pending")
        self.assertEqual(reloaded["jobs"][0]["call_id"], "fc-0")

    def test_gather_rejects_nonfinite_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            for value in (float("nan"), float("inf"), 0.0, -1.0):
                with self.subTest(value=value), self.assertRaises(ValidationError):
                    manager.gather(timeout_per_call=value)

    def test_cancel_only_pending_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}, {"seed": 1}], kind="fold")
            state = manager.cancel()
        self.assertEqual(adapter.cancelled, ["fc-0", "fc-1"])
        self.assertEqual(state["status"], "cancellation-requested")
        self.assertEqual(
            {job["status"] for job in state["jobs"]}, {"cancellation-requested"}
        )

    def test_cancel_race_preserves_late_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            manager.cancel()
            adapter.results = [
                modal_result({"ok": "completed-before-cancel"}, {"seed": 0})
            ]
            state = manager.gather()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(
            state["jobs"][0]["result"]["result"]["ok"],
            "completed-before-cancel",
        )
        self.assertIn("cancellation_race", state["jobs"][0])

    def test_provider_confirmed_cancellation_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            manager.cancel()
            adapter.results = [CancelledCallError("cancelled")]
            state = manager.gather()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["jobs"][0]["status"], "cancelled")

    def test_cancel_preserves_completed_top_level_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            adapter.results = [modal_result({"ok": True}, {"seed": 0})]
            manager.gather()
            state = manager.cancel()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(adapter.cancelled, [])

    def test_existing_state_requires_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            with self.assertRaises(ValidationError):
                manager.spawn([{"seed": 1}], kind="fold")

    def test_bare_json_result_fails_closed_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            adapter.results = [{"ok": True}]
            state = manager.gather()
        self.assertEqual(state["status"], "failed")
        self.assertIn("provenance", state["jobs"][0]["error"])

    def test_incomplete_modal_provenance_is_rejected(self) -> None:
        envelope = modal_result({"ok": True}, {"seed": 0})
        envelope["provenance"]["artifacts"] = []
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([{"seed": 0}], kind="fold")
            adapter.results = [envelope]
            state = manager.gather()
        self.assertEqual(state["status"], "failed")
        self.assertIn("checksummed artifacts", state["jobs"][0]["error"])

    def test_modal_result_must_bind_submission_and_provider(self) -> None:
        for mutate in (
            lambda value: value.update(submission_sha256="f" * 64),
            lambda value: value["provenance"].update(input_sha256="f" * 64),
            lambda value: value["provenance"].update(endpoint="modal://other/function"),
            lambda value: value["provenance"].update(esm_git_revision="0" * 40),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                payload = {"seed": 0}
                envelope = modal_result({"ok": True}, payload)
                mutate(envelope)
                adapter = FakeModal()
                manager = self.manager(directory, adapter)
                manager.spawn([payload], kind="fold")
                adapter.results = [envelope]
                state = manager.gather()
                self.assertEqual(state["status"], "failed")

    def test_modal_result_rejects_nonfinite_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = {"seed": 0}
            envelope = modal_result({"score": float("nan")}, payload)
            adapter = FakeModal()
            manager = self.manager(directory, adapter)
            manager.spawn([payload], kind="fold")
            adapter.results = [envelope]
            state = manager.gather()
        self.assertEqual(state["status"], "failed")
        self.assertIn("JSON-serializable", state["jobs"][0]["error"])

    def test_crash_window_becomes_submission_indeterminate_without_resubmit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            payload_sha256 = input_digest({"seed": 0})
            write_json_atomic(
                path,
                {
                    "schema_version": "1.2",
                    "provider": "modal",
                    "provider_identity": {
                        "app_name": "test-app",
                        "function_name": "test-function",
                    },
                    "scope": "control-plane-only",
                    "completion_contract": "result-plus-validated-provenance-envelope",
                    "kind": "fold",
                    "created_at": "2026-07-01T00:00:00Z",
                    "updated_at": "2026-07-01T00:00:00Z",
                    "status": "submitting",
                    "jobs": [
                        {
                            "index": 0,
                            "payload_sha256": payload_sha256,
                            "status": "spawning",
                            "call_id": None,
                            "result": None,
                            "error": None,
                        }
                    ],
                },
            )
            adapter = FakeModal()
            manager = ModalJobManager(adapter, ModalJobStore(path))
            state = manager.gather()
            cancelled = manager.cancel()
            persisted = manager.store.load()
        self.assertEqual(state["status"], "submission-indeterminate")
        self.assertEqual(cancelled["status"], "submission-indeterminate")
        job = persisted["jobs"][0]
        self.assertEqual(job["status"], "submission-indeterminate")
        self.assertTrue(job["manual_reconciliation"]["do_not_resubmit"])
        self.assertEqual(job["manual_reconciliation"]["payload_sha256"], payload_sha256)
        self.assertEqual(adapter.spawned, [])
        self.assertEqual(adapter.cancelled, [])


if __name__ == "__main__":
    unittest.main()
