"""Durable, adapter-driven Modal spawn/gather/cancel orchestration."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .constants import (
    ESMFOLD2_HF_MODELS,
    ESM_GIT_REVISION,
    HF_REVISIONS,
    MODAL_BINDER_ESM_GIT_REVISION,
    MODAL_BINDER_HF_REVISIONS,
    TRANSFORMERS_GIT_REVISION,
)
from .errors import ValidationError
from .provenance import input_digest, utc_now, validate_provenance, write_json_atomic
from .security import redact

MODAL_JOB_STATUSES = {
    "spawning",
    "submission-indeterminate",
    "pending",
    "cancellation-requested",
    "completed",
    "cancelled",
    "failed",
}


def mark_submission_indeterminate(
    job: dict[str, Any], identity: dict[str, str], reason: str
) -> None:
    job["status"] = "submission-indeterminate"
    job["error"] = (
        f"{reason}; provider acceptance is unknown. Do not resubmit automatically. "
        "Inspect the Modal dashboard using the recorded app/function and reconcile "
        "this payload digest manually."
    )
    job["manual_reconciliation"] = {
        "app_name": identity["app_name"],
        "function_name": identity["function_name"],
        "payload_sha256": job["payload_sha256"],
        "possible_remote_acceptance": True,
        "do_not_resubmit": True,
    }


class ModalAdapter(Protocol):
    def provider_identity(self) -> dict[str, str]: ...

    def spawn(self, payload: dict[str, Any]) -> str: ...

    def gather(self, call_ids: list[str], *, timeout: float) -> list[Any]: ...

    def cancel(self, call_id: str) -> None: ...


class CancelledCallError(RuntimeError):
    """Typed terminal result for a provider-confirmed call cancellation."""


def validate_modal_result_envelope(
    value: Any,
    *,
    submission_sha256: str,
    provider_identity: dict[str, str],
    kind: str,
) -> dict[str, Any]:
    """Require scientific results to carry complete remote provenance metadata."""

    if not isinstance(value, dict):
        raise ValidationError("Modal result must be a JSON object")
    if set(value) != {"submission_sha256", "result", "provenance"}:
        raise ValidationError(
            "Modal scientific result requires submission_sha256, result, and provenance"
        )
    if value["submission_sha256"] != submission_sha256:
        raise ValidationError("Modal result does not match the submitted payload digest")
    provenance = value["provenance"]
    if not isinstance(provenance, dict):
        raise ValidationError("Modal result provenance must be an object")
    validate_provenance(provenance)
    if "modal" not in provenance["execution_route"].split("+"):
        raise ValidationError("Modal result provenance must include the modal route")
    if provenance["input_sha256"] != submission_sha256:
        raise ValidationError("Modal result provenance is not bound to the submission")
    expected_endpoint = (
        f"modal://{provider_identity['app_name']}/{provider_identity['function_name']}"
    )
    if provenance["endpoint"] != expected_endpoint or not any(
        call["endpoint"] == expected_endpoint for call in provenance["provider_calls"]
    ):
        raise ValidationError("Modal result provenance provider identity does not match")
    if provenance["transformers_git_revision"] != TRANSFORMERS_GIT_REVISION:
        raise ValidationError("Modal ESM result requires the pinned Transformers revision")
    if kind == "fold":
        model_id = provenance["model_id"]
        if (
            model_id not in ESMFOLD2_HF_MODELS
            or provenance["model_revision"] != HF_REVISIONS[model_id]
            or provenance["esm_git_revision"] != ESM_GIT_REVISION
        ):
            raise ValidationError("Modal fold result does not match the pinned ESMFold2 stack")
    elif kind == "binder-design":
        model_id = provenance["model_id"]
        if (
            model_id not in MODAL_BINDER_HF_REVISIONS
            or provenance["model_revision"] != MODAL_BINDER_HF_REVISIONS[model_id]
            or provenance["esm_git_revision"] != MODAL_BINDER_ESM_GIT_REVISION
            or provenance["parameters"].get("model_revisions")
            != MODAL_BINDER_HF_REVISIONS
        ):
            raise ValidationError("Modal binder result does not match the pinned model stack")
    else:
        raise ValidationError("Modal job kind is invalid")
    if not provenance["artifacts"]:
        raise ValidationError("Modal result provenance requires checksummed artifacts")
    safe = redact(value)
    try:
        json.dumps(safe, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Modal result envelope must be JSON-serializable") from exc
    if not isinstance(safe, dict):  # pragma: no cover - defensive
        raise ValidationError("Modal result envelope must remain an object")
    return safe


@dataclass
class ModalJobStore:
    path: Path

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise ValidationError(f"Modal job state does not exist: {self.path}")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "1.2":
            raise ValidationError("Modal job state has an unsupported schema")
        identity = value.get("provider_identity")
        if (
            value.get("provider") != "modal"
            or not isinstance(identity, dict)
            or not isinstance(identity.get("app_name"), str)
            or not identity["app_name"]
            or not isinstance(identity.get("function_name"), str)
            or not identity["function_name"]
            or not isinstance(value.get("jobs"), list)
            or value.get("kind") not in {"fold", "binder-design"}
        ):
            raise ValidationError("Modal job state is missing validated provider identity")
        recovered = False
        for job in value["jobs"]:
            if (
                not isinstance(job, dict)
                or isinstance(job.get("index"), bool)
                or not isinstance(job.get("index"), int)
                or not isinstance(job.get("payload_sha256"), str)
                or len(job["payload_sha256"]) != 64
                or job.get("status") not in MODAL_JOB_STATUSES
            ):
                raise ValidationError("Modal job state contains an invalid job entry")
            if job["status"] in {"pending", "cancellation-requested"} and (
                not isinstance(job.get("call_id"), str) or not job["call_id"]
            ):
                raise ValidationError("pending Modal job state requires a call_id")
            if job["status"] == "spawning":
                mark_submission_indeterminate(
                    job, identity, "process ended during Modal submission"
                )
                recovered = True
        if recovered:
            value["status"] = "submission-indeterminate"
            self.save(value)
        return value

    def save(self, value: dict[str, Any]) -> None:
        value["updated_at"] = utc_now()
        write_json_atomic(self.path, value)


class ModalJobManager:
    def __init__(self, adapter: ModalAdapter, store: ModalJobStore) -> None:
        self.adapter = adapter
        self.store = store

    def spawn(self, payloads: list[dict[str, Any]], *, kind: str) -> dict[str, Any]:
        if not payloads:
            raise ValidationError("at least one Modal payload is required")
        if self.store.path.exists():
            raise ValidationError("Modal state already exists; resume it or choose a new path")
        identity = self.adapter.provider_identity()
        if not identity.get("app_name") or not identity.get("function_name"):
            raise ValidationError("Modal spawn requires app and function identity")
        state: dict[str, Any] = {
            "schema_version": "1.2",
            "provider": "modal",
            "scope": "control-plane-only",
            "completion_contract": "result-plus-validated-provenance-envelope",
            "provider_identity": identity,
            "kind": kind,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "jobs": [],
            "status": "submitting",
        }
        self.store.save(state)
        for index, payload in enumerate(payloads):
            job = {
                "index": index,
                "payload_sha256": input_digest(payload),
                "status": "spawning",
                "call_id": None,
                "result": None,
                "error": None,
                "spawn_started_at": utc_now(),
                "spawn_finished_at": None,
                "finished_at": None,
            }
            state["jobs"].append(job)
            self.store.save(state)
            try:
                job["call_id"] = self.adapter.spawn(payload)
                job["status"] = "pending"
            except Exception as exc:  # provider adapters define their own exceptions
                reason = f"Modal spawn returned {type(exc).__name__}"
                mark_submission_indeterminate(job, identity, reason)
                job["spawn_error"] = redact(str(exc))[:1000]
                job["finished_at"] = utc_now()
            job["spawn_finished_at"] = utc_now()
            self.store.save(state)
        state["status"] = self._aggregate_status(state)
        self.store.save(state)
        return state

    @staticmethod
    def _aggregate_status(state: dict[str, Any]) -> str:
        statuses = {job["status"] for job in state["jobs"]}
        if "submission-indeterminate" in statuses:
            return "submission-indeterminate"
        if statuses == {"completed"}:
            return "completed"
        if statuses == {"cancelled"}:
            return "cancelled"
        if statuses == {"failed"}:
            return "failed"
        if statuses.issubset({"pending", "cancellation-requested"}):
            return (
                "cancellation-requested"
                if "cancellation-requested" in statuses
                else "pending"
            )
        if "pending" in statuses and not statuses.intersection({"completed", "cancelled"}):
            return "pending"
        return "partial"

    def gather(self, *, timeout_per_call: float = 1800.0) -> dict[str, Any]:
        if not math.isfinite(timeout_per_call) or timeout_per_call <= 0:
            raise ValidationError(
                "Modal gather timeout_per_call must be finite and positive"
            )
        state = self.store.load()
        pending = [
            job
            for job in state["jobs"]
            if job["status"] in {"pending", "cancellation-requested"}
        ]
        if not pending:
            return state
        for job in pending:
            call_id = job["call_id"]
            if not isinstance(call_id, str) or not call_id:
                raise ValidationError("pending Modal jobs require call_id")
            try:
                results = self.adapter.gather([call_id], timeout=timeout_per_call)
            except Exception as exc:
                job["last_poll_error"] = redact(str(exc))[:1000]
                state["last_gather_error"] = job["last_poll_error"]
                self.store.save(state)
                continue
            if len(results) != 1:
                job["last_poll_error"] = "Modal gather returned an unexpected result count"
                self.store.save(state)
                continue
            result = results[0]
            previous_status = job["status"]
            job.pop("last_poll_error", None)
            if isinstance(result, CancelledCallError):
                job["status"] = "cancelled"
                job["error"] = None
                job["finished_at"] = utc_now()
            elif isinstance(result, BaseException):
                job["status"] = "failed"
                job["error"] = redact(str(result))[:1000]
                job["finished_at"] = utc_now()
            else:
                try:
                    safe_result = validate_modal_result_envelope(
                        result,
                        submission_sha256=job["payload_sha256"],
                        provider_identity=state["provider_identity"],
                        kind=state["kind"],
                    )
                except ValidationError as exc:
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    job["finished_at"] = utc_now()
                else:
                    job["status"] = "completed"
                    job["result"] = safe_result
                    job["finished_at"] = utc_now()
                    if previous_status == "cancellation-requested":
                        job["cancellation_race"] = "completed-before-cancellation-took-effect"
            self.store.save(state)
        state["status"] = self._aggregate_status(state)
        self.store.save(state)
        return state

    def cancel(self) -> dict[str, Any]:
        state = self.store.load()
        for job in state["jobs"]:
            if job["status"] != "pending":
                continue
            try:
                self.adapter.cancel(job["call_id"])
                job["status"] = "cancellation-requested"
                job["cancellation_requested_at"] = utc_now()
            except Exception as exc:
                job["error"] = redact(str(exc))[:1000]
            self.store.save(state)
        state["status"] = self._aggregate_status(state)
        self.store.save(state)
        return state


class ModalFunctionAdapter:
    """Optional real adapter for a deployed Modal function accepting kwargs."""

    def __init__(self, app_name: str | None = None, function_name: str | None = None) -> None:
        try:
            import modal
        except ImportError as exc:
            raise ValidationError("install the Modal SDK before using the real adapter") from exc
        self.modal = modal
        self.app_name = app_name
        self.function_name = function_name
        self.function = None

    def provider_identity(self) -> dict[str, str]:
        return {
            "app_name": self.app_name or "",
            "function_name": self.function_name or "",
        }

    def spawn(self, payload: dict[str, Any]) -> str:
        if not self.app_name or not self.function_name:
            raise ValidationError("Modal spawn requires app and function names")
        if self.function is None:
            self.function = self.modal.Function.from_name(
                self.app_name, self.function_name
            )
        call = self.function.spawn(**payload)
        return call.object_id

    def gather(self, call_ids: list[str], *, timeout: float) -> list[Any]:
        calls = [self.modal.FunctionCall.from_id(call_id) for call_id in call_ids]
        results: list[Any] = []
        for call in calls:
            try:
                results.append(call.get(timeout=timeout))
            except TimeoutError:
                raise
            except self.modal.exception.InputCancellation:
                results.append(CancelledCallError("Modal call was cancelled"))
            except Exception as exc:  # preserve partial results across remote failures
                results.append(exc)
        return results

    def cancel(self, call_id: str) -> None:
        self.modal.FunctionCall.from_id(call_id).cancel()
