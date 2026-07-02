#!/usr/bin/env python3
"""Biohub ESM deterministic helper CLI.

The CLI reads credentials only from the environment or an existing Modal
profile. It never accepts credential values as arguments and never prints them.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from biohub_esm_lib.atlas import AtlasClient, safe_download_endpoint
from biohub_esm_lib.atlas_jobs import AtlasBatchStore
from biohub_esm_lib.constants import (
    ATLAS_API_PREFIX,
    BIOHUB_BASE_URL,
    ESM_GIT_REVISION,
    HF_REVISIONS,
    MODAL_BINDER_ESM_GIT_REVISION,
    MODAL_BINDER_EXAMPLE_REVISION,
    MODAL_BINDER_HF_REVISIONS,
    MODAL_BINDER_SOURCE_SHA256,
    TRANSFORMERS_GIT_REVISION,
)
from biohub_esm_lib.errors import APIError, BiohubESMError, SchemaDriftError, ValidationError
from biohub_esm_lib.http import BiohubClient
from biohub_esm_lib.modal_jobs import ModalFunctionAdapter, ModalJobManager, ModalJobStore
from biohub_esm_lib.provenance import (
    artifact_record,
    build_provenance,
    input_digest,
    materialize_pdb_fields,
    sha256_bytes,
    utc_now,
    verify_installed_vcs_revision,
    write_bytes_atomic,
    write_json_atomic,
)
from biohub_esm_lib.routing import RouteRequest, route_request
from biohub_esm_lib.security import credential_preflight, redact
from biohub_esm_lib.validation import (
    sequence_md5,
    validate_batch_hashes,
    validate_atlas_fold_sequence,
    validate_atlas_search_sequence,
    validate_esmc_sequence,
    validate_fold_input,
    validate_hosted_fold_config,
    validate_md5,
)


def load_json(path: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValidationError(f"input JSON contains non-finite constant: {value}")

    if path == "-":
        return json.load(sys.stdin, parse_constant=reject_constant)
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=reject_constant)


def emit(value: Any) -> None:
    print(
        json.dumps(
            redact(value),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _persist_schema_drift(args: argparse.Namespace, exc: SchemaDriftError) -> None:
    """Retain a redacted diagnostic artifact without echoing the payload to stderr."""

    output_dir_value = getattr(args, "output_dir", None)
    output_value = getattr(args, "output", None)
    if exc.raw is None or (not output_dir_value and not output_value):
        return
    if output_dir_value:
        output_dir = Path(output_dir_value).resolve()
        prefix = "schema-drift-raw"
    else:
        output_path = Path(output_value).resolve()
        output_dir = output_path.parent
        prefix = f"{output_path.name}.schema-drift-raw"
    raw = exc.raw
    if isinstance(raw, bytes):
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Only the public Atlas client is permitted to persist opaque bytes.
            if getattr(args, "command", None) != "atlas":
                return
            path = output_dir / f"{prefix}.bin"
            write_bytes_atomic(path, raw)
        else:
            path = output_dir / f"{prefix}.txt"
            write_bytes_atomic(path, str(redact(decoded)).encode("utf-8"))
    else:
        path = output_dir / f"{prefix}.json"
        write_json_atomic(path, redact(raw))
    exc.diagnostic_path = str(path)


def _save_atlas_artifact_provenance(
    artifact_path: Path,
    *,
    media_type: str,
    endpoint_path: str,
    inputs: Any,
    parameters: dict[str, Any],
    started_at: str,
    base_url: str = BIOHUB_BASE_URL,
    provider_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    artifact = artifact_record(artifact_path, media_type=media_type)
    provenance = build_provenance(
        route="atlas-api",
        endpoint=f"{base_url.rstrip('/')}{ATLAS_API_PREFIX}{endpoint_path}",
        model_id=None,
        model_revision=None,
        inputs=inputs,
        parameters=parameters,
        seed=None,
        started_at=started_at,
        artifacts=[artifact],
        provider_calls=provider_calls,
    )
    provenance_path = artifact_path.with_name(f"{artifact_path.name}.provenance.json")
    write_json_atomic(provenance_path, provenance)
    return {
        "artifact": artifact,
        "provenance": str(provenance_path),
        "provenance_artifact": artifact_record(
            provenance_path, media_type="application/json"
        ),
    }


def _atlas_endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{ATLAS_API_PREFIX}{path}"


def _safe_download_endpoint(url: str) -> str:
    return safe_download_endpoint(url)


def _batch_provider_call(
    *, endpoint: str, operation: str, http_status: int | None
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "operation": operation,
        "http_status": http_status,
        "timestamp": utc_now(),
    }


def _persist_atlas_batch_provenance(
    store: AtlasBatchStore, state: dict[str, Any]
) -> dict[str, Any]:
    state_artifact = artifact_record(store.path, media_type="application/json")
    provenance = build_provenance(
        route="atlas-api",
        endpoint=state["endpoint"],
        model_id=None,
        model_revision=None,
        inputs={"job_id": state["job_id"], "status": state["status"]},
        input_sha256=state["request"]["input_sha256"],
        parameters=state["request"]["parameters"],
        seed=None,
        started_at=state["submitted_at"],
        artifacts=[state_artifact, *state["artifacts"]],
        provider_calls=state["provider_calls"],
        finished_at=state["updated_at"],
    )
    write_json_atomic(store.provenance_path, provenance)
    return {
        "state": str(store.path),
        "state_artifact": state_artifact,
        "provenance": str(store.provenance_path),
        "job": state,
    }


def _load_or_adopt_atlas_batch(
    args: argparse.Namespace,
) -> tuple[AtlasBatchStore, str, dict[str, Any]]:
    store = AtlasBatchStore(Path(args.state).resolve())
    if store.path.is_file():
        job_id = store.resolve_job_id(getattr(args, "job_id", None))
        return store, job_id, store.load()
    requested = getattr(args, "job_id", None)
    if not requested:
        raise ValidationError(
            "Atlas batch state is missing; provide --job-id once to adopt an existing job"
        )
    endpoint = _atlas_endpoint(
        args.base_url, f"/proteins/batch/jobs/{requested}"
    )
    state = store.adopt(job_id=requested, endpoint=endpoint)
    _persist_atlas_batch_provenance(store, state)
    return store, requested, state


def _bool_flag(parser: argparse.ArgumentParser, name: str, help_text: str) -> None:
    parser.add_argument(name, action="store_true", help=help_text)


def _strip_embedded_pdb(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "pdb" and isinstance(item, str):
                result["pdb_summary"] = {
                    "embedded": True,
                    "size_bytes": len(item.encode("utf-8")),
                    "sha256": sha256_bytes(item.encode("utf-8")),
                    "note": "pass --output-dir to preserve the structure artifact",
                }
            else:
                result[key] = _strip_embedded_pdb(item)
        return result
    if isinstance(value, list):
        return [_strip_embedded_pdb(item) for item in value]
    return value


def _save_atlas_result(
    result: dict[str, Any],
    *,
    output_dir: Path,
    operation: str,
    endpoint_path: str,
    inputs: Any,
    parameters: dict[str, Any],
    started_at: str,
    base_url: str = BIOHUB_BASE_URL,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw-response.json"
    write_json_atomic(raw_path, result)
    normalized, pdb_artifacts = materialize_pdb_fields(result, output_dir, prefix=operation)
    result_path = output_dir / "result.json"
    write_json_atomic(result_path, normalized)
    artifacts = [
        artifact_record(raw_path, media_type="application/json"),
        artifact_record(result_path, media_type="application/json"),
        *pdb_artifacts,
    ]
    confidence: dict[str, Any] = {}
    if operation == "search":
        confidence["similarity_scores"] = [
            hit.get("similarity_score")
            for hit in result.get("similar_proteins", [])
            if isinstance(hit, dict) and "similarity_score" in hit
        ]
    else:
        for key in ("mean_plddt", "ptm", "cluster_pct_characterized"):
            if key in result:
                confidence[key] = result[key]
    provenance = build_provenance(
        route="atlas-api",
        endpoint=f"{base_url.rstrip('/')}{ATLAS_API_PREFIX}{endpoint_path}",
        model_id=None,
        model_revision=None,
        inputs=inputs,
        parameters=parameters,
        seed=None,
        started_at=started_at,
        artifacts=artifacts,
        confidence_metrics=confidence,
    )
    provenance_path = output_dir / "provenance.json"
    write_json_atomic(provenance_path, provenance)
    return {
        "result": normalized,
        "artifacts": [*artifacts, artifact_record(provenance_path, media_type="application/json")],
        "provenance": str(provenance_path),
    }


def command_preflight(_: argparse.Namespace) -> None:
    emit(credential_preflight())


def command_pins(_: argparse.Namespace) -> None:
    emit(
        {
            "esm_git_revision": ESM_GIT_REVISION,
            "transformers_git_revision": TRANSFORMERS_GIT_REVISION,
            "hugging_face_revisions": HF_REVISIONS,
            "modal_binder": {
                "modal_examples_revision": MODAL_BINDER_EXAMPLE_REVISION,
                "esm_git_revision": MODAL_BINDER_ESM_GIT_REVISION,
                "model_revisions": MODAL_BINDER_HF_REVISIONS,
                "source_sha256": MODAL_BINDER_SOURCE_SHA256,
            },
        }
    )


def command_verify_install(_: argparse.Namespace) -> None:
    emit(
        {
            "esm_git_revision": verify_installed_vcs_revision(
                "esm", ESM_GIT_REVISION
            ),
            "transformers_git_revision": verify_installed_vcs_revision(
                "transformers", TRANSFORMERS_GIT_REVISION
            ),
        }
    )


def command_route(args: argparse.Namespace) -> None:
    request = RouteRequest(
        task=args.task,
        item_count=args.item_count,
        long_running=args.long_running,
        bulk_dataset=args.bulk_dataset,
        private=args.private,
        offline=args.offline,
        data_residency=args.data_residency,
        custom_model=args.custom_model,
        fine_tune=args.fine_tune,
        sustained_workload=args.sustained_workload,
        has_msa=args.has_msa,
        accuracy_priority=args.accuracy_priority,
        owns_gpu=args.owns_gpu,
    )
    emit(route_request(request).as_dict())


def command_validate_sequence(args: argparse.Namespace) -> None:
    value = Path(args.sequence_file).read_text(encoding="utf-8") if args.sequence_file else args.sequence
    if value is None:
        raise ValidationError("provide --sequence or --sequence-file")
    if args.target == "esmc":
        normalized = validate_esmc_sequence(value)
    elif args.target == "atlas-search":
        normalized = validate_atlas_search_sequence(value)
    else:
        normalized = validate_atlas_fold_sequence(value)
    emit(
        {
            "valid": True,
            "target": args.target,
            "residues": len(normalized),
            "input_sha256": input_digest(normalized),
            "md5": sequence_md5(normalized),
        }
    )


def command_validate_fold(args: argparse.Namespace) -> None:
    payload = load_json(args.input)
    if not isinstance(payload, dict):
        raise ValidationError("fold input JSON must be an object")
    normalized = validate_fold_input(payload, model=args.model, require_msa=args.require_msa)
    config: dict[str, Any] = {}
    if args.config:
        raw_config = load_json(args.config)
        if not isinstance(raw_config, dict):
            raise ValidationError("fold config JSON must be an object")
        config = validate_hosted_fold_config(raw_config)
    emit(
        {
            "valid": True,
            "model": args.model,
            "entity_count": len(normalized["sequences"]),
            "config": config,
            "input_sha256": input_digest(normalized),
        }
    )


def command_managed_post(args: argparse.Namespace) -> None:
    payload = load_json(args.input)
    if not isinstance(payload, dict):
        raise ValidationError("managed request input must be a JSON object")
    client = BiohubClient(
        token=os.environ.get("ESM_API_KEY", ""),
        base_url=BIOHUB_BASE_URL,
        timeout=args.timeout,
    )
    started = utc_now()
    result = client.post(args.endpoint, payload)
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
        raw_path = output_dir / "raw-response.json"
        write_json_atomic(raw_path, redact(result))
        model_id = str(payload.get("model")) if payload.get("model") else None
        provenance = build_provenance(
            route="biohub",
            endpoint=f"{BIOHUB_BASE_URL}/api/v1/{args.endpoint}",
            model_id=model_id,
            model_revision=model_id,
            inputs=payload,
            parameters={key: value for key, value in payload.items() if key not in {"inputs", "all_atom_input"}},
            seed=payload.get("seed") if isinstance(payload.get("seed"), int) else None,
            started_at=started,
            artifacts=[artifact_record(raw_path, media_type="application/json")],
        )
        provenance_path = output_dir / "provenance.json"
        write_json_atomic(provenance_path, provenance)
        emit({"result": _strip_embedded_pdb(result), "provenance": str(provenance_path)})
    else:
        emit(_strip_embedded_pdb(result))


def command_atlas_search(args: argparse.Namespace) -> None:
    started = utc_now()
    sequence = validate_atlas_search_sequence(args.sequence)
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    result = client.search(
        sequence,
        topk_results=args.topk_results,
        topk_features=args.topk_features,
        min_similarity=args.min_similarity,
        cluster_pct_characterized_max=args.cluster_pct_characterized_max,
        include_cluster_info=args.include_cluster_info,
    )
    params = {
        "topk_results": args.topk_results,
        "topk_features": args.topk_features,
        "min_similarity": args.min_similarity,
        "cluster_pct_characterized_max": args.cluster_pct_characterized_max,
        "include_cluster_info": args.include_cluster_info,
    }
    if args.output_dir:
        emit(
            _save_atlas_result(
                result,
                output_dir=Path(args.output_dir).resolve(),
                operation="search",
                endpoint_path="/similarity-search",
                inputs=sequence,
                parameters=params,
                started_at=started,
                base_url=args.base_url,
            )
        )
    else:
        emit(_strip_embedded_pdb(result))


def command_atlas_protein(args: argparse.Namespace) -> None:
    started = utc_now()
    protein_hash = validate_md5(args.protein_hash)
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    fold_sequence = None
    if args.fold_on_miss:
        if args.sequence_file:
            fold_sequence = validate_atlas_fold_sequence(
                Path(args.sequence_file).read_text(encoding="utf-8")
            )
        elif args.sequence:
            fold_sequence = validate_atlas_fold_sequence(args.sequence)
    result = client.protein(
        protein_hash,
        topk_features=args.topk_features,
        fold_on_miss=args.fold_on_miss,
        normalize_features=not args.raw_features,
        feature_indices=args.feature_index,
        sequence_for_fold=fold_sequence,
    )
    params = {
        "topk_features": args.topk_features,
        "fold_on_miss": args.fold_on_miss,
        "normalize_features": not args.raw_features,
        "feature_indices": args.feature_index,
        "fold_sequence_sha256": input_digest(fold_sequence) if fold_sequence else None,
    }
    if args.output_dir:
        emit(
            _save_atlas_result(
                result,
                output_dir=Path(args.output_dir).resolve(),
                operation="protein",
                endpoint_path=f"/proteins/{protein_hash}",
                inputs={
                    "protein_hash": protein_hash,
                    "fold_sequence_sha256": input_digest(fold_sequence) if fold_sequence else None,
                },
                parameters=params,
                started_at=started,
                base_url=args.base_url,
            )
        )
    else:
        emit(_strip_embedded_pdb(result))


def command_atlas_cluster(args: argparse.Namespace) -> None:
    started = utc_now()
    protein_hash = validate_md5(args.protein_hash)
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    result = client.cluster(protein_hash, topk_features=args.topk_features)
    if args.output_dir:
        emit(
            _save_atlas_result(
                result,
                output_dir=Path(args.output_dir).resolve(),
                operation="cluster",
                endpoint_path=f"/clusters/{protein_hash}",
                inputs={"protein_hash": protein_hash},
                parameters={"topk_features": args.topk_features},
                started_at=started,
                base_url=args.base_url,
            )
        )
    else:
        emit(_strip_embedded_pdb(result))


def command_atlas_features(args: argparse.Namespace) -> None:
    started = utc_now()
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    result = client.features()
    if args.output_dir:
        emit(
            _save_atlas_result(
                result,
                output_dir=Path(args.output_dir).resolve(),
                operation="features",
                endpoint_path="/features",
                inputs={"catalog": "ESM Atlas 16,384 SAE features"},
                parameters={},
                started_at=started,
                base_url=args.base_url,
            )
        )
    else:
        emit(result)


def command_atlas_feature(args: argparse.Namespace) -> None:
    started = utc_now()
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    result = client.feature(args.feature_index)
    if args.output_dir:
        emit(
            _save_atlas_result(
                result,
                output_dir=Path(args.output_dir).resolve(),
                operation=f"feature-{args.feature_index}",
                endpoint_path=f"/features/{args.feature_index}",
                inputs={"feature_index": args.feature_index},
                parameters={},
                started_at=started,
                base_url=args.base_url,
            )
        )
    else:
        emit(result)


def command_atlas_thumbnail(args: argparse.Namespace) -> None:
    started = utc_now()
    protein_hash = validate_md5(args.protein_hash)
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    response = client.thumbnail(protein_hash, args.thumbnail_type)
    destination = Path(args.output).resolve()
    write_bytes_atomic(destination, response.body)
    emit(
        _save_atlas_artifact_provenance(
            destination,
            media_type="image/png",
            endpoint_path=(
                f"/proteins/{protein_hash}/thumbnail/{args.thumbnail_type}"
            ),
            inputs={"protein_hash": protein_hash},
            parameters={"thumbnail_type": args.thumbnail_type},
            started_at=started,
            base_url=args.base_url,
        )
    )


def command_atlas_batch_submit(args: argparse.Namespace) -> None:
    started = utc_now()
    raw_hashes = load_json(args.hashes)
    if not isinstance(raw_hashes, list):
        raise ValidationError("batch hashes file must contain a JSON list")
    hashes = validate_batch_hashes(raw_hashes)
    parameters = {
        "topk_features": args.topk_features,
        "include_structure": not args.no_structure,
        "include_cluster_info": not args.no_cluster_info,
        "include_sequence": not args.no_sequence,
        "include_features": {
            "protein_level": not args.no_features,
            "per_residue": not args.no_features
            and not args.no_per_residue_features,
        },
    }
    store = AtlasBatchStore(Path(args.state).resolve())
    if store.path.exists():
        raise ValidationError("refusing to overwrite an existing Atlas batch state")
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    status, result, _ = client.submit_batch(
        hashes,
        **parameters,
    )
    submit_endpoint = _atlas_endpoint(args.base_url, "/proteins/batch")
    provider_call = _batch_provider_call(
        endpoint=submit_endpoint, operation="submit", http_status=status
    )
    if status == 200:
        path = Path(args.output).resolve()
        write_bytes_atomic(path, result)
        evidence = _save_atlas_artifact_provenance(
            path,
            media_type="application/zip",
            endpoint_path="/proteins/batch",
            inputs={"protein_hashes": hashes},
            parameters=parameters,
            started_at=started,
            base_url=args.base_url,
            provider_calls=[provider_call],
        )
        state = store.initialize(
            job_id=None,
            status="completed",
            submitted_at=started,
            endpoint=submit_endpoint,
            input_sha256=input_digest(hashes),
            parameters=parameters,
            http_status=status,
            response={"status": "completed", "delivery": "synchronous"},
            provider_call=provider_call,
            artifacts=[evidence["artifact"], evidence["provenance_artifact"]],
        )
    else:
        job_id = result["job_id"]
        state = store.initialize(
            job_id=job_id,
            status=result["status"],
            submitted_at=started,
            endpoint=_atlas_endpoint(
                args.base_url, f"/proteins/batch/jobs/{job_id}"
            ),
            input_sha256=input_digest(hashes),
            parameters=parameters,
            http_status=status,
            response=result,
            provider_call=provider_call,
        )
    emit({"http_status": status, **_persist_atlas_batch_provenance(store, state)})


def command_atlas_batch_status(args: argparse.Namespace) -> None:
    store, job_id, _ = _load_or_adopt_atlas_batch(args)
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    status, result = client.batch_status(job_id)
    state = store.update(
        response=result,
        http_status=status,
        provider_call=_batch_provider_call(
            endpoint=_atlas_endpoint(
                args.base_url, f"/proteins/batch/jobs/{job_id}"
            ),
            operation="status",
            http_status=status,
        ),
    )
    emit({"http_status": status, **_persist_atlas_batch_provenance(store, state)})


def command_atlas_batch_cancel(args: argparse.Namespace) -> None:
    store, job_id, current = _load_or_adopt_atlas_batch(args)
    if current["status"] in {"completed", "failed", "expired", "cancelled"}:
        emit(
            {
                "cancel": "no-op-terminal-state",
                **_persist_atlas_batch_provenance(store, current),
            }
        )
        return
    if current["status"] == "cancellation-requested":
        emit(
            {
                "cancel": "already-requested",
                **_persist_atlas_batch_provenance(store, current),
            }
        )
        return
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    client.cancel_batch(job_id)
    state = store.update(
        response={"job_id": job_id, "status": "cancellation-requested"},
        http_status=204,
        status="cancellation-requested",
        provider_call=_batch_provider_call(
            endpoint=_atlas_endpoint(
                args.base_url, f"/proteins/batch/jobs/{job_id}"
            ),
            operation="cancel",
            http_status=204,
        ),
    )
    emit(_persist_atlas_batch_provenance(store, state))


def command_atlas_batch_wait(args: argparse.Namespace) -> None:
    started = utc_now()
    if (
        not math.isfinite(args.poll_interval)
        or not math.isfinite(args.poll_timeout)
        or args.poll_interval <= 0
        or args.poll_timeout <= 0
    ):
        raise ValidationError("poll interval and timeout must be finite and positive")
    store, job_id, _ = _load_or_adopt_atlas_batch(args)
    client = AtlasClient(base_url=args.base_url, timeout=args.timeout)
    deadline = time.monotonic() + args.poll_timeout
    while True:
        http_status, result = client.batch_status(job_id)
        state = store.update(
            response=result,
            http_status=http_status,
            provider_call=_batch_provider_call(
                endpoint=_atlas_endpoint(
                    args.base_url, f"/proteins/batch/jobs/{job_id}"
                ),
                operation="status",
                http_status=http_status,
            ),
        )
        _persist_atlas_batch_provenance(store, state)
        if result["status"] != "pending":
            break
        if time.monotonic() >= deadline:
            raise APIError(
                status=None,
                kind="timeout",
                message="Atlas batch polling timed out; durable state is preserved",
            )
        time.sleep(
            min(args.poll_interval, max(0.0, deadline - time.monotonic()))
        )
    if result.get("status") == "completed" and args.output and result.get("download_url"):
        destination = Path(args.output).resolve()
        result["download"] = client.download(result["download_url"], destination)
        status_endpoint = _atlas_endpoint(
            args.base_url, f"/proteins/batch/jobs/{job_id}"
        )
        download_endpoint = _safe_download_endpoint(result["download_url"])
        evidence = _save_atlas_artifact_provenance(
            destination,
            media_type="application/zip",
            endpoint_path=f"/proteins/batch/jobs/{job_id}",
            inputs={"job_id": job_id},
            parameters={
                "poll_interval": args.poll_interval,
                "poll_timeout": args.poll_timeout,
                "resumed": result["download"]["resumed"],
            },
            started_at=started,
            base_url=args.base_url,
            provider_calls=[
                {"endpoint": status_endpoint, "operation": "status"},
                {"endpoint": download_endpoint, "operation": "download"},
            ],
        )
        state = store.update(
            response=result,
            http_status=result["download"]["http_status"],
            provider_call=_batch_provider_call(
                endpoint=download_endpoint,
                operation="download",
                http_status=result["download"]["http_status"],
            ),
            artifacts=[evidence["artifact"], evidence["provenance_artifact"]],
        )
    emit(_persist_atlas_batch_provenance(store, state))


def _modal_manager(args: argparse.Namespace) -> ModalJobManager:
    adapter = ModalFunctionAdapter(
        getattr(args, "app_name", None), getattr(args, "function_name", None)
    )
    return ModalJobManager(adapter, ModalJobStore(Path(args.state).resolve()))


def command_modal_spawn(args: argparse.Namespace) -> None:
    payloads = load_json(args.input)
    if not isinstance(payloads, list) or any(not isinstance(item, dict) for item in payloads):
        raise ValidationError("Modal input must be a JSON list of objects")
    if not args.confirm_cost:
        raise ValidationError("Modal ESM submission requires --confirm-cost after visible cost review")
    if args.max_jobs < 1 or len(payloads) > args.max_jobs:
        raise ValidationError("Modal payload count exceeds the explicitly confirmed --max-jobs")
    emit(_modal_manager(args).spawn(payloads, kind=args.kind))


def command_modal_gather(args: argparse.Namespace) -> None:
    emit(_modal_manager(args).gather(timeout_per_call=args.timeout_per_call))


def command_modal_cancel(args: argparse.Namespace) -> None:
    emit(_modal_manager(args).cancel())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="report configured/missing credentials without values")
    preflight.set_defaults(func=command_preflight)
    pins = sub.add_parser("pins", help="print pinned upstream revisions")
    pins.set_defaults(func=command_pins)
    verify_install = sub.add_parser(
        "verify-install",
        help="verify installed ESM and Transformers requested/resolved commits",
    )
    verify_install.set_defaults(func=command_verify_install)

    route = sub.add_parser("route", help="select the scientifically appropriate execution route")
    route.add_argument("--task", choices=["atlas", "esmc", "fold", "binder-design"], required=True)
    route.add_argument("--item-count", type=int, default=1)
    for flag, help_text in (
        ("--long-running", "workflow requires durable long-running execution"),
        ("--bulk-dataset", "request is for public Atlas bulk data"),
        ("--private", "input cannot leave user-owned infrastructure"),
        ("--offline", "execution must be offline or air-gapped"),
        ("--data-residency", "execution has data-residency constraints"),
        ("--custom-model", "custom model or head is required"),
        ("--fine-tune", "fine-tuning is required"),
        ("--sustained-workload", "workload is sustained rather than interactive"),
        ("--has-msa", "an MSA is available and should condition folding"),
        ("--accuracy-priority", "prefer accuracy over throughput"),
        ("--owns-gpu", "user owns suitable GPU infrastructure"),
    ):
        _bool_flag(route, flag, help_text)
    route.set_defaults(func=command_route)

    sequence = sub.add_parser("validate-sequence", help="validate and digest a sequence")
    sequence.add_argument("--target", choices=["esmc", "atlas-search", "atlas-fold"], required=True)
    source = sequence.add_mutually_exclusive_group(required=True)
    source.add_argument("--sequence")
    source.add_argument("--sequence-file")
    sequence.set_defaults(func=command_validate_sequence)

    fold = sub.add_parser("validate-fold", help="validate ESMFold2 input and hosted parameters")
    fold.add_argument("--input", required=True)
    fold.add_argument("--config")
    fold.add_argument("--model", required=True)
    fold.add_argument("--require-msa", action="store_true")
    fold.set_defaults(func=command_validate_fold)

    managed = sub.add_parser("managed-post", help="send an allowlisted managed API JSON contract")
    managed.add_argument("--endpoint", choices=["encode", "logits", "fold", "fold_all_atom"], required=True)
    managed.add_argument("--input", required=True)
    managed.add_argument("--output-dir")
    managed.add_argument("--timeout", type=float, default=120)
    managed.set_defaults(func=command_managed_post)

    atlas = sub.add_parser("atlas", help="call the public Atlas alpha API")
    atlas.set_defaults(base_url=BIOHUB_BASE_URL)
    atlas.add_argument("--timeout", type=float, default=120)
    atlas_sub = atlas.add_subparsers(dest="atlas_command", required=True)

    search = atlas_sub.add_parser("search")
    search.add_argument("--sequence", required=True)
    search.add_argument("--topk-results", type=int, default=10)
    search.add_argument("--topk-features", type=int, default=20)
    search.add_argument("--min-similarity", type=float, default=0.5)
    search.add_argument("--cluster-pct-characterized-max", type=int)
    search.add_argument("--include-cluster-info", action="store_true")
    search.add_argument("--output-dir")
    search.set_defaults(func=command_atlas_search)

    protein = atlas_sub.add_parser("protein")
    protein.add_argument("--protein-hash", required=True)
    protein.add_argument("--topk-features", type=int, default=10)
    protein.add_argument("--fold-on-miss", action="store_true")
    protein_sequence = protein.add_mutually_exclusive_group()
    protein_sequence.add_argument("--sequence")
    protein_sequence.add_argument("--sequence-file")
    protein.add_argument("--raw-features", action="store_true")
    protein.add_argument("--feature-index", type=int, action="append")
    protein.add_argument("--output-dir")
    protein.set_defaults(func=command_atlas_protein)

    cluster = atlas_sub.add_parser("cluster")
    cluster.add_argument("--protein-hash", required=True)
    cluster.add_argument("--topk-features", type=int, default=10)
    cluster.add_argument("--output-dir")
    cluster.set_defaults(func=command_atlas_cluster)

    features = atlas_sub.add_parser("features")
    features.add_argument("--output-dir")
    features.set_defaults(func=command_atlas_features)
    feature = atlas_sub.add_parser("feature")
    feature.add_argument("--feature-index", type=int, required=True)
    feature.add_argument("--output-dir")
    feature.set_defaults(func=command_atlas_feature)
    thumbnail = atlas_sub.add_parser("thumbnail")
    thumbnail.add_argument("--protein-hash", required=True)
    thumbnail.add_argument("--thumbnail-type", choices=["pct-characterized", "plddt"], required=True)
    thumbnail.add_argument("--output", required=True)
    thumbnail.set_defaults(func=command_atlas_thumbnail)

    batch_submit = atlas_sub.add_parser("batch-submit")
    batch_submit.add_argument("--hashes", required=True)
    batch_submit.add_argument("--topk-features", type=int, default=10)
    batch_submit.add_argument("--no-structure", action="store_true")
    batch_submit.add_argument("--no-cluster-info", action="store_true")
    batch_submit.add_argument("--no-sequence", action="store_true")
    batch_submit.add_argument("--no-features", action="store_true")
    batch_submit.add_argument("--no-per-residue-features", action="store_true")
    batch_submit.add_argument("--output", required=True)
    batch_submit.add_argument("--state", required=True)
    batch_submit.set_defaults(func=command_atlas_batch_submit)
    batch_status = atlas_sub.add_parser("batch-status")
    batch_status.add_argument("--state", required=True)
    batch_status.add_argument("--job-id")
    batch_status.set_defaults(func=command_atlas_batch_status)
    batch_cancel = atlas_sub.add_parser("batch-cancel")
    batch_cancel.add_argument("--state", required=True)
    batch_cancel.add_argument("--job-id")
    batch_cancel.set_defaults(func=command_atlas_batch_cancel)
    batch_wait = atlas_sub.add_parser("batch-wait")
    batch_wait.add_argument("--state", required=True)
    batch_wait.add_argument("--job-id")
    batch_wait.add_argument("--poll-interval", type=float, default=5)
    batch_wait.add_argument("--poll-timeout", type=float, default=1800)
    batch_wait.add_argument("--output")
    batch_wait.set_defaults(func=command_atlas_batch_wait)

    modal = sub.add_parser("modal-jobs", help="durable spawn/gather/cancel for a deployed Modal function")
    modal_sub = modal.add_subparsers(dest="modal_command", required=True)
    for name, function in (
        ("spawn", command_modal_spawn),
        ("gather", command_modal_gather),
        ("cancel", command_modal_cancel),
    ):
        command = modal_sub.add_parser(name)
        command.add_argument("--state", required=True)
        if name == "spawn":
            command.add_argument("--app-name", required=True)
            command.add_argument("--function-name", required=True)
            command.add_argument("--input", required=True)
            command.add_argument("--kind", choices=["fold", "binder-design"], required=True)
            command.add_argument("--max-jobs", type=int, required=True)
            command.add_argument("--confirm-cost", action="store_true")
        elif name == "gather":
            command.add_argument("--timeout-per-call", type=float, default=1800)
        command.set_defaults(func=function)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except SchemaDriftError as exc:
        _persist_schema_drift(args, exc)
        print(
            json.dumps(
                {"error": exc.as_dict()}, indent=2, sort_keys=True, allow_nan=False
            ),
            file=sys.stderr,
        )
        return 2
    except BiohubESMError as exc:
        error = exc.as_dict() if hasattr(exc, "as_dict") else {"kind": exc.__class__.__name__, "message": str(exc)}
        print(
            json.dumps(
                redact({"error": error}), indent=2, sort_keys=True, allow_nan=False
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"error": {"kind": exc.__class__.__name__, "message": redact(str(exc))}},
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
