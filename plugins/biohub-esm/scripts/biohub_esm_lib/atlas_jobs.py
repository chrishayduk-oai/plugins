"""Durable, credential-safe state for public Atlas batch jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atlas import validate_job_id
from .errors import ValidationError
from .provenance import input_digest, utc_now, write_json_atomic
from .security import redact

ATLAS_BATCH_STATE_SCHEMA = "1.0"
ATLAS_BATCH_PROVIDER = "biohub-esm-atlas-v1alpha1"
ATLAS_BATCH_STATUSES = {
    "pending",
    "completed",
    "cancelled",
    "cancellation-requested",
    "failed",
    "expired",
}


def _safe_response(value: dict[str, Any]) -> dict[str, Any]:
    """Keep alpha-schema diagnostics while never persisting download capabilities."""

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): (
                    "[EPHEMERAL URL OMITTED]"
                    if str(key).lower() == "download_url"
                    else visit(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    sanitized = redact(visit(value))
    if not isinstance(sanitized, dict):  # pragma: no cover - defensive
        raise ValidationError("Atlas batch response must be an object")
    return sanitized


class AtlasBatchStore:
    """Atomic job-state persistence suitable for polling across CLI invocations."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def provenance_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.provenance.json")

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise ValidationError(f"Atlas batch state does not exist: {self.path}")
        try:
            import json

            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError("Atlas batch state is not readable JSON") from exc
        self.validate(value)
        return value

    def save(self, value: dict[str, Any]) -> dict[str, Any]:
        value = redact(value)
        self.validate(value)
        write_json_atomic(self.path, value)
        return value

    @staticmethod
    def validate(value: Any) -> None:
        if not isinstance(value, dict):
            raise ValidationError("Atlas batch state must be a JSON object")
        required = {
            "schema_version",
            "provider",
            "job_id",
            "status",
            "submitted_at",
            "updated_at",
            "endpoint",
            "request",
            "last_http_status",
            "last_response",
            "provider_calls",
            "artifacts",
            "provenance_path",
        }
        missing = sorted(required - set(value))
        if missing:
            raise ValidationError(
                f"Atlas batch state is missing: {', '.join(missing)}"
            )
        if value["schema_version"] != ATLAS_BATCH_STATE_SCHEMA:
            raise ValidationError("unsupported Atlas batch state schema")
        if value["provider"] != ATLAS_BATCH_PROVIDER:
            raise ValidationError("Atlas batch state provider is invalid")
        if value["job_id"] is not None:
            validate_job_id(value["job_id"])
        if value["status"] not in ATLAS_BATCH_STATUSES:
            raise ValidationError("Atlas batch state status is invalid")
        request = value["request"]
        if not isinstance(request, dict) or not isinstance(
            request.get("input_sha256"), str
        ) or len(request["input_sha256"]) != 64:
            raise ValidationError("Atlas batch state request digest is invalid")
        if not isinstance(request.get("parameters"), dict):
            raise ValidationError("Atlas batch state parameters must be an object")
        if not isinstance(value["provider_calls"], list) or not value["provider_calls"]:
            raise ValidationError("Atlas batch state requires provider calls")
        if not isinstance(value["last_response"], dict):
            raise ValidationError("Atlas batch state last_response must be an object")
        if not isinstance(value["artifacts"], list):
            raise ValidationError("Atlas batch state artifacts must be a list")

    def initialize(
        self,
        *,
        job_id: str | None,
        status: str,
        submitted_at: str,
        endpoint: str,
        input_sha256: str,
        parameters: dict[str, Any],
        http_status: int | None,
        response: dict[str, Any],
        provider_call: dict[str, Any],
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if job_id is not None:
            validate_job_id(job_id)
        now = utc_now()
        return self.save(
            {
                "schema_version": ATLAS_BATCH_STATE_SCHEMA,
                "provider": ATLAS_BATCH_PROVIDER,
                "job_id": job_id,
                "status": status,
                "submitted_at": submitted_at,
                "updated_at": now,
                "endpoint": endpoint,
                "request": {
                    "input_sha256": input_sha256,
                    "parameters": redact(parameters),
                },
                "last_http_status": http_status,
                "last_response": _safe_response(response),
                "provider_calls": [redact(provider_call)],
                "artifacts": artifacts or [],
                "provenance_path": str(self.provenance_path),
            }
        )

    def adopt(self, *, job_id: str, endpoint: str) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        now = utc_now()
        return self.initialize(
            job_id=job_id,
            status="pending",
            submitted_at=now,
            endpoint=endpoint,
            input_sha256=input_digest({"adopted_job_id": job_id}),
            parameters={"adopted": True},
            http_status=None,
            response={"job_id": job_id, "status": "pending", "adopted": True},
            provider_call={
                "endpoint": endpoint,
                "operation": "adopt",
                "timestamp": now,
            },
        )

    def update(
        self,
        *,
        response: dict[str, Any],
        http_status: int | None,
        provider_call: dict[str, Any],
        status: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        response_job_id = response.get("job_id")
        if response_job_id is not None and response_job_id != state["job_id"]:
            raise ValidationError("Atlas batch response job_id does not match state")
        resolved_status = status or response.get("status")
        if resolved_status not in ATLAS_BATCH_STATUSES:
            raise ValidationError("Atlas batch update status is invalid")
        if (
            state["status"] == "cancellation-requested"
            and resolved_status == "pending"
        ):
            # A pending provider response has not revoked the user's intent.
            # Preserve it until Atlas reports a terminal race outcome.
            resolved_status = "cancellation-requested"
        state["status"] = resolved_status
        state["updated_at"] = utc_now()
        state["last_http_status"] = http_status
        state["last_response"] = _safe_response(response)
        state["provider_calls"].append(redact(provider_call))
        if artifacts:
            state["artifacts"].extend(redact(artifacts))
        return self.save(state)

    def resolve_job_id(self, requested: str | None) -> str:
        state = self.load()
        state_job_id = state["job_id"]
        if state_job_id is None:
            raise ValidationError("synchronous Atlas batch state has no resumable job_id")
        if requested is not None and validate_job_id(requested) != state_job_id:
            raise ValidationError("--job-id does not match the durable Atlas batch state")
        return state_job_id
