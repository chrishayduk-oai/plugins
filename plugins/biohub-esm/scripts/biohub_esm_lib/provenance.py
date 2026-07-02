"""Artifact integrity and provenance sidecars."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import (
    ESMC_MANAGED_MODELS,
    ESMFOLD2_MANAGED_MODELS,
    HF_REVISIONS,
    MODAL_BINDER_HF_REVISIONS,
)
from .errors import ValidationError
from .security import redact

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ALL_HF_REVISIONS = {**HF_REVISIONS, **MODAL_BINDER_HF_REVISIONS}
MANAGED_MODEL_IDS = {*ESMC_MANAGED_MODELS, *ESMFOLD2_MANAGED_MODELS}
BASE_EXECUTION_ROUTES = {"atlas-api", "atlas-s3", "biohub", "modal", "self-hosted"}
HTTP_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}


def _utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"provenance {field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"provenance {field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationError(f"provenance {field} must be a UTC timestamp")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "value must be interoperable JSON without non-finite numbers"
        ) from exc
    return rendered.encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def input_digest(value: Any) -> str:
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_json(value)
    return sha256_bytes(payload)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "JSON artifact contains a non-serializable or non-finite value"
        ) from exc
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def artifact_record(path: Path, *, media_type: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if media_type:
        result["media_type"] = media_type
    return result


def verify_installed_vcs_revision(distribution_name: str, expected: str) -> str:
    """Verify a PEP 610 direct-VCS install before asserting its commit in provenance."""

    if not GIT_SHA_RE.fullmatch(expected):
        raise ValidationError("expected VCS revision must be a full 40-character commit SHA")
    try:
        distribution = metadata.distribution(distribution_name)
        direct_url_text = distribution.read_text("direct_url.json")
        direct_url = json.loads(direct_url_text or "null")
    except (metadata.PackageNotFoundError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"{distribution_name} is not an inspectable direct-VCS install"
        ) from exc
    vcs_info = direct_url.get("vcs_info") if isinstance(direct_url, dict) else None
    actual = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    requested = vcs_info.get("requested_revision") if isinstance(vcs_info, dict) else None
    if actual != expected or requested != expected:
        raise ValidationError(
            f"{distribution_name} installed/requested VCS revision does not match the required pin"
        )
    return expected


def build_provenance(
    *,
    route: str,
    endpoint: str,
    model_id: str | None,
    model_revision: str | None,
    inputs: Any,
    parameters: dict[str, Any],
    seed: int | None,
    started_at: str,
    artifacts: list[dict[str, Any]],
    confidence_metrics: dict[str, Any] | None = None,
    provider_calls: list[dict[str, Any]] | None = None,
    finished_at: str | None = None,
    esm_git_revision: str | None = None,
    transformers_git_revision: str | None = None,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    resolved_revision = model_revision
    revision_kind = "not-applicable"
    if model_id in ALL_HF_REVISIONS and resolved_revision is None:
        resolved_revision = ALL_HF_REVISIONS[model_id]
    if model_id in ALL_HF_REVISIONS:
        if resolved_revision != ALL_HF_REVISIONS[model_id]:
            raise ValidationError("Hugging Face model revision does not match the validated pin")
        revision_kind = "hugging-face-commit"
    elif model_id in MANAGED_MODEL_IDS and "biohub" in route.split("+"):
        resolved_revision = model_id
        revision_kind = "versioned-managed-model-id"
    elif model_id is not None and "biohub" in route.split("+"):
        raise ValidationError("Biohub provenance requires a supported managed model ID")
    elif model_id is not None:
        revision_kind = "explicit"
    result = {
        "schema_version": "1.0",
        "execution_route": route,
        "endpoint": endpoint,
        "provider_calls": redact(provider_calls or [{"endpoint": endpoint}]),
        "model_id": model_id,
        "model_revision": resolved_revision,
        "model_revision_kind": revision_kind,
        "esm_git_revision": esm_git_revision,
        "transformers_git_revision": transformers_git_revision,
        "input_sha256": input_sha256 or input_digest(inputs),
        "parameters": redact(parameters),
        "seed": seed,
        "started_at": started_at,
        "finished_at": finished_at or utc_now(),
        "artifacts": artifacts,
        "confidence_metrics": redact(confidence_metrics or {}),
    }
    validate_provenance(result)
    return result


def validate_provenance(value: dict[str, Any]) -> None:
    canonical_json(value)
    required = {
        "schema_version",
        "execution_route",
        "endpoint",
        "provider_calls",
        "model_id",
        "model_revision",
        "model_revision_kind",
        "esm_git_revision",
        "transformers_git_revision",
        "input_sha256",
        "parameters",
        "seed",
        "started_at",
        "finished_at",
        "artifacts",
        "confidence_metrics",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValidationError(f"provenance is missing required fields: {', '.join(missing)}")
    if value["schema_version"] != "1.0":
        raise ValidationError("unsupported provenance schema version")
    if not SHA256_RE.fullmatch(str(value["input_sha256"])):
        raise ValidationError("provenance input_sha256 is invalid")
    route = value["execution_route"]
    if not isinstance(route, str) or not route:
        raise ValidationError("provenance execution_route is invalid")
    route_parts = route.split("+")
    if (
        any(part not in BASE_EXECUTION_ROUTES for part in route_parts)
        or len(set(route_parts)) != len(route_parts)
    ):
        raise ValidationError("provenance execution_route is invalid")
    if not isinstance(value["endpoint"], str) or not value["endpoint"].strip():
        raise ValidationError("provenance endpoint must be non-empty")
    started_at = _utc_timestamp(value["started_at"], "started_at")
    finished_at = _utc_timestamp(value["finished_at"], "finished_at")
    if finished_at < started_at:
        raise ValidationError("provenance finished_at precedes started_at")
    seed = value["seed"]
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValidationError("provenance seed must be null or an integer")
    provider_calls = value["provider_calls"]
    if not isinstance(provider_calls, list) or not provider_calls:
        raise ValidationError("provenance provider_calls must be a non-empty list")
    for call in provider_calls:
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("endpoint"), str)
            or not call["endpoint"].strip()
        ):
            raise ValidationError("each provenance provider call requires an endpoint")
        if "parameters" in call and not isinstance(call["parameters"], dict):
            raise ValidationError("provider call parameters must be an object")
        if "method" in call and call["method"] not in HTTP_METHODS:
            raise ValidationError("provider call method is invalid")
        if "operation" in call and (
            not isinstance(call["operation"], str) or not call["operation"].strip()
        ):
            raise ValidationError("provider call operation must be non-empty")
        if "http_status" in call and call["http_status"] is not None and (
            isinstance(call["http_status"], bool)
            or not isinstance(call["http_status"], int)
            or not 100 <= call["http_status"] <= 599
        ):
            raise ValidationError("provider call http_status is invalid")
        for timestamp_field in ("timestamp", "started_at", "finished_at"):
            if timestamp_field in call:
                _utc_timestamp(
                    call[timestamp_field], f"provider_calls.{timestamp_field}"
                )
        if "started_at" in call and "finished_at" in call and _utc_timestamp(
            call["finished_at"], "provider_calls.finished_at"
        ) < _utc_timestamp(call["started_at"], "provider_calls.started_at"):
            raise ValidationError("provider call finished_at precedes started_at")
    revision_kind = value["model_revision_kind"]
    if revision_kind not in {
        "not-applicable",
        "hugging-face-commit",
        "versioned-managed-model-id",
        "explicit",
    }:
        raise ValidationError("provenance model_revision_kind is invalid")
    model_id = value["model_id"]
    model_revision = value["model_revision"]
    if revision_kind == "not-applicable":
        if model_id is not None or model_revision is not None:
            raise ValidationError("not-applicable model provenance requires null model fields")
    elif revision_kind == "hugging-face-commit":
        if model_id not in ALL_HF_REVISIONS or model_revision != ALL_HF_REVISIONS[model_id]:
            raise ValidationError("Hugging Face model provenance does not match a validated pin")
    elif revision_kind == "versioned-managed-model-id":
        if model_id not in MANAGED_MODEL_IDS or model_revision != model_id:
            raise ValidationError("managed model provenance requires its exact versioned ID")
    elif (
        not isinstance(model_id, str)
        or not model_id.strip()
        or not isinstance(model_revision, str)
        or not model_revision.strip()
        or model_id in ALL_HF_REVISIONS
        or model_id in MANAGED_MODEL_IDS
    ):
        raise ValidationError("explicit model provenance requires a non-managed ID and revision")
    for field in ("esm_git_revision", "transformers_git_revision"):
        revision = value[field]
        if revision is not None and not GIT_SHA_RE.fullmatch(str(revision)):
            raise ValidationError(f"provenance {field} must be null or a full commit SHA")
    if not isinstance(value["artifacts"], list):
        raise ValidationError("provenance artifacts must be a list")
    for artifact in value["artifacts"]:
        if not isinstance(artifact, dict):
            raise ValidationError("each provenance artifact must be an object")
        if not isinstance(artifact.get("path"), str) or not artifact["path"].strip():
            raise ValidationError("each provenance artifact requires a path")
        size = artifact.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValidationError("each provenance artifact requires a nonnegative size")
        if not isinstance(artifact.get("media_type"), str) or not artifact[
            "media_type"
        ].strip():
            raise ValidationError("each provenance artifact requires a media type")
        if not SHA256_RE.fullmatch(str(artifact.get("sha256", ""))):
            raise ValidationError("each provenance artifact requires a SHA-256 checksum")
    if not isinstance(value["parameters"], dict) or not isinstance(value["confidence_metrics"], dict):
        raise ValidationError("provenance parameters and confidence_metrics must be objects")


def materialize_pdb_fields(payload: Any, output_dir: Path, *, prefix: str) -> tuple[Any, list[dict[str, Any]]]:
    """Replace embedded PDB strings with checksummed artifact references."""

    artifacts: list[dict[str, Any]] = []
    counter = 0

    def visit(value: Any) -> Any:
        nonlocal counter
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key == "pdb" and isinstance(item, str) and item.strip():
                    counter += 1
                    path = output_dir / f"{prefix}-{counter}.pdb"
                    write_bytes_atomic(path, item.encode("utf-8"))
                    record = artifact_record(path, media_type="chemical/x-pdb")
                    artifacts.append(record)
                    result["pdb_artifact"] = record
                else:
                    result[key] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(payload), artifacts
