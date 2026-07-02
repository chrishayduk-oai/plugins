#!/usr/bin/env python3
"""Opt-in live smokes for public, managed, Modal, and self-hosted routes.

No command accepts a credential. Provider clients read existing environment or
native profiles, and persisted diagnostics are recursively redacted.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm"
TEST_ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.atlas import AtlasClient
from biohub_esm_lib.constants import (
    ESM_GIT_REVISION,
    ESMC_MANAGED_MODELS,
    HF_REVISIONS,
    MODAL_BINDER_ESM_GIT_REVISION,
    MODAL_BINDER_HF_REVISIONS,
    TRANSFORMERS_GIT_REVISION,
)
from biohub_esm_lib.binder_smoke import (
    BINDER_VOLUME_NAMES,
    prepare_official_binder_example,
)
from biohub_esm_lib.errors import APIError, BiohubESMError, ValidationError
from biohub_esm_lib.provenance import (
    artifact_record,
    build_provenance,
    materialize_pdb_fields,
    input_digest,
    utc_now,
    verify_installed_vcs_revision,
    write_json_atomic,
)
from biohub_esm_lib.security import redact
from biohub_esm_lib.validation import sequence_md5, validate_atlas_fold_sequence

BENIGN_SEQUENCE = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
SMALL_SEQUENCE = "MKTAYIAKQRQISFVKSHFSRQ"
MODAL_FOLD_VOLUME_NAMES = ("lsc110-biohub-esm-fold-models-v1",)
MODAL_VOLUME_LIST_PRICE_PER_GIB_MONTH = 0.09


def cleanup_modal_volumes(modal_cli: str, volume_names: tuple[str, ...]) -> dict[str, Any]:
    results = []
    for name in volume_names:
        try:
            completed = subprocess.run(
                [modal_cli, "volume", "delete", name, "--allow-missing", "--yes"],
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append(
                redact(
                    {
                        "volume": name,
                        "returncode": None,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            )
            continue
        results.append(
            redact(
                {
                    "volume": name,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
        )
    return {
        "policy": "delete-after-smoke",
        "all_succeeded": all(result["returncode"] == 0 for result in results),
        "results": results,
    }


def run_modal_command_with_cleanup(
    command: list[str],
    *,
    modal_cli: str,
    volume_names: tuple[str, ...],
    cwd: Path | None = None,
    timeout: float,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        completed = subprocess.CompletedProcess(
            command, 124, stdout, f"{stderr}\nlocal smoke timeout"
        )
    finally:
        cleanup = cleanup_modal_volumes(modal_cli, volume_names)
    if completed is None:
        raise RuntimeError("Modal command ended without a completion record")
    return completed, cleanup


def require_env(name: str) -> None:
    if not os.environ.get(name, "").strip():
        raise APIError(
            status=None,
            kind="missing-credentials",
            message=f"{name} is missing in this login shell",
        )


def managed_provider_call(
    *,
    endpoint: str,
    operation: str,
    inputs: Any,
    parameters: dict[str, Any],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Record one managed request without retaining raw biological payloads."""

    return {
        "endpoint": endpoint,
        "operation": operation,
        "input_sha256": input_digest(inputs),
        "parameters": parameters,
        "parameters_sha256": input_digest(parameters),
        "started_at": started_at,
        "finished_at": finished_at,
    }


def save_result(
    output_dir: Path,
    *,
    result: dict[str, Any],
    route: str,
    endpoint: str,
    model_id: str | None,
    model_revision: str | None,
    inputs: Any,
    parameters: dict[str, Any],
    seed: int | None,
    started_at: str,
    extra_artifacts: list[dict[str, Any]] | None = None,
    confidence: dict[str, Any] | None = None,
    provider_calls: list[dict[str, Any]] | None = None,
    esm_git_revision: str | None = None,
    transformers_git_revision: str | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"
    write_json_atomic(result_path, redact(result))
    artifacts = [artifact_record(result_path, media_type="application/json"), *(extra_artifacts or [])]
    provenance = build_provenance(
        route=route,
        endpoint=endpoint,
        model_id=model_id,
        model_revision=model_revision,
        inputs=inputs,
        parameters=parameters,
        seed=seed,
        started_at=started_at,
        artifacts=artifacts,
        confidence_metrics=confidence,
        provider_calls=provider_calls,
        esm_git_revision=esm_git_revision,
        transformers_git_revision=transformers_git_revision,
    )
    provenance_path = output_dir / "provenance.json"
    write_json_atomic(provenance_path, provenance)
    return {
        "status": "passed",
        "result": str(result_path),
        "provenance": str(provenance_path),
        "artifacts": [*artifacts, artifact_record(provenance_path, media_type="application/json")],
    }


def smoke_atlas(output_dir: Path) -> dict[str, Any]:
    started = utc_now()
    client = AtlasClient(timeout=180)
    provider_calls: list[dict[str, Any]] = []
    call_started = utc_now()
    search_raw = client.search(
        BENIGN_SEQUENCE,
        topk_results=2,
        topk_features=5,
        include_cluster_info=True,
    )
    provider_calls.append(
        {
            "method": "GET",
            "endpoint": "https://biohub.ai/esm/protein/api/v1alpha1/similarity-search",
            "started_at": call_started,
            "finished_at": utc_now(),
            "parameters": {
                "input_sha256": input_digest(BENIGN_SEQUENCE),
                "topk_results": 2,
                "topk_features": 5,
                "include_cluster_info": True,
            },
        }
    )
    if not search_raw["similar_proteins"]:
        raise ValidationError("Atlas live smoke returned no hits for the benign control")
    search_path = output_dir / "search-raw.json"
    write_json_atomic(search_path, search_raw)
    search, structures = materialize_pdb_fields(search_raw, output_dir, prefix="search-hit")
    hit_hash = search_raw["similar_proteins"][0]["protein_hash"]
    call_started = utc_now()
    protein = client.protein(hit_hash, topk_features=5, fold_on_miss=False)
    provider_calls.append(
        {
            "method": "GET",
            "endpoint": (
                "https://biohub.ai/esm/protein/api/v1alpha1/proteins/"
                f"{hit_hash}"
            ),
            "started_at": call_started,
            "finished_at": utc_now(),
            "parameters": {"topk_features": 5, "fold_on_miss": False},
        }
    )
    protein_path = output_dir / "protein-raw.json"
    write_json_atomic(protein_path, protein)
    protein_normalized, protein_structures = materialize_pdb_fields(
        protein, output_dir, prefix="protein"
    )
    representative = protein.get("cluster_rep_protein_hash") or hit_hash
    call_started = utc_now()
    cluster = client.cluster(representative, topk_features=5)
    provider_calls.append(
        {
            "method": "GET",
            "endpoint": (
                "https://biohub.ai/esm/protein/api/v1alpha1/clusters/"
                f"{representative}"
            ),
            "started_at": call_started,
            "finished_at": utc_now(),
            "parameters": {"topk_features": 5},
        }
    )
    cluster_path = output_dir / "cluster.json"
    write_json_atomic(cluster_path, cluster)
    result = {
        "query_sha256": input_digest(BENIGN_SEQUENCE),
        "hit_count": len(search_raw["similar_proteins"]),
        "first_hit": {
            "protein_hash": hit_hash,
            "similarity_score": search_raw["similar_proteins"][0].get("similarity_score"),
        },
        "protein": protein_normalized,
        "cluster": {
            "protein_hash": cluster.get("protein_hash"),
            "cluster_size": cluster.get("cluster_size"),
            "member_count_returned": len(cluster.get("member_protein_hashes", [])),
        },
        "alpha_schema": True,
    }
    return save_result(
        output_dir,
        result=result,
        route="atlas-api",
        endpoint="https://biohub.ai/esm/protein/api/v1alpha1",
        model_id=None,
        model_revision=None,
        inputs=BENIGN_SEQUENCE,
        parameters={"topk_results": 2, "topk_features": 5, "include_cluster_info": True},
        seed=None,
        started_at=started,
        extra_artifacts=[
            artifact_record(search_path, media_type="application/json"),
            artifact_record(protein_path, media_type="application/json"),
            artifact_record(cluster_path, media_type="application/json"),
            *structures,
            *protein_structures,
        ],
        confidence={"first_hit_similarity": result["first_hit"]["similarity_score"]},
        provider_calls=provider_calls,
    )


def smoke_biohub_esmc(output_dir: Path) -> dict[str, Any]:
    require_env("ESM_API_KEY")
    started = utc_now()
    try:
        from esm.sdk import esmc_client
        from esm.sdk.api import ESMProtein, LogitsConfig
    except ImportError as exc:
        raise ValidationError("pinned Biohub esm SDK is not installed") from exc
    esm_revision = verify_installed_vcs_revision("esm", ESM_GIT_REVISION)

    model_id = "esmc-300m-2024-12"
    client = esmc_client(
        model=model_id,
        url="https://biohub.ai",
        token=os.environ["ESM_API_KEY"],
        request_timeout=120,
    )
    encode_parameters = {"model": model_id}
    encode_started = utc_now()
    encoded = client.encode(ESMProtein(sequence=SMALL_SEQUENCE))
    encode_finished = utc_now()
    logits_parameters = {
        "model": model_id,
        "sequence": True,
        "return_embeddings": True,
        "return_mean_embedding": True,
    }
    logits_started = utc_now()
    output = client.logits(
        encoded,
        LogitsConfig(sequence=True, return_embeddings=True, return_mean_embedding=True),
    )
    logits_finished = utc_now()
    if hasattr(output, "error_code"):
        raise APIError(status=None, kind="provider", message="Biohub ESMC returned a model error")
    sequence_logits = output.logits.sequence if output.logits is not None else None
    result = {
        "model_id": model_id,
        "sequence_logits_shape": list(sequence_logits.shape) if sequence_logits is not None else None,
        "embeddings_shape": list(output.embeddings.shape) if output.embeddings is not None else None,
        "mean_embedding_shape": list(output.mean_embedding.shape) if output.mean_embedding is not None else None,
        "input_sha256": input_digest(SMALL_SEQUENCE),
    }
    provider_calls = [
        managed_provider_call(
            endpoint="https://biohub.ai/api/v1/encode",
            operation="encode",
            inputs=SMALL_SEQUENCE,
            parameters=encode_parameters,
            started_at=encode_started,
            finished_at=encode_finished,
        ),
        managed_provider_call(
            endpoint="https://biohub.ai/api/v1/logits",
            operation="logits",
            inputs={
                "encoded_sequence_sha256": input_digest(SMALL_SEQUENCE),
                "model": model_id,
            },
            parameters=logits_parameters,
            started_at=logits_started,
            finished_at=logits_finished,
        ),
    ]
    return save_result(
        output_dir,
        result=result,
        route="biohub",
        endpoint="https://biohub.ai/api/v1/logits",
        model_id=model_id,
        model_revision=model_id,
        inputs=SMALL_SEQUENCE,
        parameters=logits_parameters,
        seed=None,
        started_at=started,
        provider_calls=provider_calls,
        esm_git_revision=esm_revision,
    )


def smoke_biohub_esmfold2(output_dir: Path) -> dict[str, Any]:
    require_env("ESM_API_KEY")
    started = utc_now()
    try:
        from esm.sdk import esmfold2_client
        from esm.sdk.api import ESMProteinError, FoldingConfig
        from esm.utils.structure.input_builder import ProteinInput, StructurePredictionInput
    except ImportError as exc:
        raise ValidationError("pinned Biohub esm SDK is not installed") from exc
    esm_revision = verify_installed_vcs_revision("esm", ESM_GIT_REVISION)

    model_id = "esmfold2-fast-2026-05"
    client = esmfold2_client(
        model=model_id,
        url="https://biohub.ai",
        token=os.environ["ESM_API_KEY"],
        request_timeout=180,
    )
    fold_input = StructurePredictionInput(sequences=[ProteinInput(id="A", sequence=SMALL_SEQUENCE)])
    normalized_input = {
        "sequences": [{"type": "protein", "id": "A", "sequence": SMALL_SEQUENCE}]
    }
    fold_parameters = {"num_loops": 3, "num_sampling_steps": 20, "include_pae": True}
    config = FoldingConfig(**fold_parameters)
    fold_started = utc_now()
    output = client.fold_all_atom(fold_input, config=config)
    fold_finished = utc_now()
    if isinstance(output, ESMProteinError):
        raise APIError(status=None, kind="provider", message="Biohub ESMFold2 returned a model error")
    output_dir.mkdir(parents=True, exist_ok=True)
    cif_path = output_dir / "prediction.cif"
    cif_path.write_text(output.complex.to_mmcif(), encoding="utf-8")
    plddt_mean = float(output.plddt.float().mean()) if output.plddt is not None else None
    result = {
        "model_id": model_id,
        "mean_plddt": plddt_mean,
        "ptm": float(output.ptm) if output.ptm is not None else None,
        "iptm": float(output.iptm) if output.iptm is not None else None,
        "pae_shape": list(output.pae.shape) if output.pae is not None else None,
        "input_sha256": input_digest(normalized_input),
        "sequence_sha256": input_digest(SMALL_SEQUENCE),
        "static_hypothesis": True,
    }
    provider_calls = [
        managed_provider_call(
            endpoint="https://biohub.ai/api/v1/fold_all_atom",
            operation="fold_all_atom",
            inputs=normalized_input,
            parameters={"model": model_id, **fold_parameters},
            started_at=fold_started,
            finished_at=fold_finished,
        )
    ]
    return save_result(
        output_dir,
        result=result,
        route="biohub",
        endpoint="https://biohub.ai/api/v1/fold_all_atom",
        model_id=model_id,
        model_revision=model_id,
        inputs=normalized_input,
        parameters=fold_parameters,
        seed=None,
        started_at=started,
        extra_artifacts=[artifact_record(cif_path, media_type="chemical/x-mmcif")],
        confidence={"mean_plddt": plddt_mean, "ptm": result["ptm"], "iptm": result["iptm"]},
        provider_calls=provider_calls,
        esm_git_revision=esm_revision,
    )


def smoke_hf_esmc(output_dir: Path) -> dict[str, Any]:
    started = utc_now()
    try:
        import torch
        from transformers import AutoModelForMaskedLM, AutoTokenizer
    except ImportError as exc:
        raise ValidationError("torch and the pinned Biohub transformers fork are required") from exc
    transformers_revision = verify_installed_vcs_revision(
        "transformers", TRANSFORMERS_GIT_REVISION
    )
    repo = "biohub/ESMC-300M"
    revision = HF_REVISIONS[repo]
    tokenizer = AutoTokenizer.from_pretrained(repo, revision=revision)
    model = AutoModelForMaskedLM.from_pretrained(repo, revision=revision).eval()
    encoded = tokenizer([SMALL_SEQUENCE], return_tensors="pt")
    with torch.inference_mode():
        output = model(**encoded, output_hidden_states=True)
    result = {
        "model_id": repo,
        "revision": revision,
        "logits_shape": list(output.logits.shape),
        "hidden_state_count": 0 if output.hidden_states is None else len(output.hidden_states),
        "input_sha256": input_digest(SMALL_SEQUENCE),
        "device": str(model.device),
    }
    return save_result(
        output_dir,
        result=result,
        route="self-hosted",
        endpoint=f"huggingface://{repo}",
        model_id=repo,
        model_revision=revision,
        inputs=SMALL_SEQUENCE,
        parameters={"output_hidden_states": True},
        seed=None,
        started_at=started,
        transformers_git_revision=transformers_revision,
    )


def smoke_hf_esmfold2(output_dir: Path) -> dict[str, Any]:
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    try:
        import torch
    except ImportError as exc:
        raise ValidationError("torch is required") from exc
    if not torch.cuda.is_available():
        return {"status": "skipped", "reason": "CUDA hardware is unavailable"}
    started = utc_now()
    from transformers.models.esmfold2.modeling_esmfold2 import ESMFold2Model

    transformers_revision = verify_installed_vcs_revision(
        "transformers", TRANSFORMERS_GIT_REVISION
    )

    repo = "biohub/ESMFold2-Fast"
    revision = HF_REVISIONS[repo]
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    torch.use_deterministic_algorithms(True)
    model = ESMFold2Model.from_pretrained(repo, revision=revision).cuda().eval()
    with torch.inference_mode():
        output = model.infer_protein(SMALL_SEQUENCE, num_loops=1, num_sampling_steps=5)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdb_path = output_dir / "prediction.pdb"
    pdb_path.write_text(model.output_to_pdb(output), encoding="utf-8")
    result = {
        "model_id": repo,
        "revision": revision,
        "mean_plddt": float(output["plddt"].float().mean()),
        "ptm": float(output["ptm"].float().mean()),
        "device": "cuda",
    }
    return save_result(
        output_dir,
        result=result,
        route="self-hosted",
        endpoint=f"huggingface://{repo}",
        model_id=repo,
        model_revision=revision,
        inputs=SMALL_SEQUENCE,
        parameters={
            "num_loops": 1,
            "num_sampling_steps": 5,
            "deterministic_algorithms": True,
        },
        seed=0,
        started_at=started,
        extra_artifacts=[artifact_record(pdb_path, media_type="chemical/x-pdb")],
        confidence={"mean_plddt": result["mean_plddt"], "ptm": result["ptm"]},
        esm_git_revision=None,
        transformers_git_revision=transformers_revision,
    )


def require_fresh_output_dir(output_dir: Path, context: str) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValidationError(
            f"{context} requires an empty output directory to prevent stale evidence"
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def load_strict_result(path: Path, context: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite constant: {value}")

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=reject_constant
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{context} result is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{context} result must be a JSON object")
    return value


def validate_modal_fold_result(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "mean_plddt",
        "ptm",
        "iptm",
        "model_id",
        "model_revision",
        "esm_git_revision",
        "transformers_git_revision",
        "seed",
        "input_sha256",
    }
    if set(value) != required:
        raise ValidationError("Modal fold result schema does not match the reviewed smoke")
    expected = {
        "model_id": "biohub/ESMFold2-Fast",
        "model_revision": HF_REVISIONS["biohub/ESMFold2-Fast"],
        "esm_git_revision": ESM_GIT_REVISION,
        "transformers_git_revision": TRANSFORMERS_GIT_REVISION,
        "seed": 0,
        "input_sha256": input_digest(SMALL_SEQUENCE),
    }
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise ValidationError(f"Modal fold result {field} does not match its pin")
    for field in ("mean_plddt", "ptm", "iptm"):
        metric = value[field]
        if metric is None and field in {"ptm", "iptm"}:
            continue
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(metric)
            or not 0 <= metric <= 1
        ):
            raise ValidationError(f"Modal fold result {field} must be finite on [0, 1]")
    return value


def run_modal_fold(args: argparse.Namespace) -> dict[str, Any]:
    h100_gpu_upper_bound_usd = 1.3164
    volume_planning_ceiling_gib = 20.0
    if not args.confirm_cost:
        raise ValidationError(
            "Modal fold smoke requires --confirm-cost after reviewing the cost estimate"
        )
    if (
        not math.isfinite(args.max_gpu_cost_usd)
        or args.max_gpu_cost_usd <= 0
        or args.max_gpu_cost_usd < h100_gpu_upper_bound_usd
    ):
        raise ValidationError(
            "--max-gpu-cost-usd is below the twenty-minute H100 GPU upper-bound estimate"
        )
    if not args.confirm_volume_cleanup:
        raise ValidationError(
            "Modal fold smoke requires --confirm-volume-cleanup for its ticket-scoped Volume"
        )
    started = utc_now()
    output_dir = Path(args.output_dir).resolve()
    require_fresh_output_dir(output_dir, "Modal fold smoke")
    script = TEST_ROOT / "modal_esmfold2_smoke.py"
    command = [args.modal_cli, "run", str(script), "--output-dir", str(output_dir)]
    completed, cleanup = run_modal_command_with_cleanup(
        command,
        modal_cli=args.modal_cli,
        volume_names=MODAL_FOLD_VOLUME_NAMES,
        timeout=25 * 60,
    )
    logs = redact(
        {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "volume_cleanup": cleanup,
        }
    )
    write_json_atomic(output_dir / "modal-log.json", logs)
    if not cleanup["all_succeeded"]:
        raise ValidationError(
            "Modal fold smoke could not confirm deletion of its ticket-scoped Volume"
        )
    if completed.returncode != 0:
        if "payment method" in (completed.stderr + completed.stdout).lower():
            raise ValidationError(
                "Modal GPU allocation is blocked because the workspace lacks a payment method; "
                "the pinned image build completed but inference did not start"
            )
        raise ValidationError("Modal ESMFold2 smoke failed; see redacted modal-log.json")
    result_path = output_dir / "modal-result.json"
    cif_path = output_dir / "prediction.cif"
    if not result_path.is_file() or not cif_path.is_file():
        raise ValidationError("Modal smoke did not produce expected artifacts")
    result = validate_modal_fold_result(
        load_strict_result(result_path, "Modal fold")
    )
    try:
        first_cif_line = cif_path.open(encoding="utf-8").readline().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("Modal fold mmCIF artifact is unreadable") from exc
    if not first_cif_line.startswith("data_"):
        raise ValidationError("Modal fold mmCIF artifact lacks a data block")
    return save_result(
        output_dir,
        result=result,
        route="modal",
        endpoint="modal://biohub-esm-esmfold2-smoke/ESMFold2Smoke.fold",
        model_id=result["model_id"],
        model_revision=result["model_revision"],
        inputs=SMALL_SEQUENCE,
        parameters={
            "num_loops": 3,
            "num_sampling_steps": 20,
            "h100_gpu_upper_bound_usd": h100_gpu_upper_bound_usd,
            "confirmed_gpu_cost_ceiling_usd": args.max_gpu_cost_usd,
            "modal_volumes": list(MODAL_FOLD_VOLUME_NAMES),
            "volume_planning_ceiling_gib": volume_planning_ceiling_gib,
            "volume_list_price_per_gib_month": MODAL_VOLUME_LIST_PRICE_PER_GIB_MONTH,
            "volume_retained_monthly_list_upper_bound_usd": (
                volume_planning_ceiling_gib
                * MODAL_VOLUME_LIST_PRICE_PER_GIB_MONTH
            ),
            "volume_cleanup_policy": "delete-after-smoke",
            "volume_deletion_billing_lag_days_max": 4,
        },
        seed=result["seed"],
        started_at=started,
        extra_artifacts=[
            artifact_record(cif_path, media_type="chemical/x-mmcif"),
            artifact_record(output_dir / "modal-log.json", media_type="application/json"),
        ],
        confidence={key: result.get(key) for key in ("mean_plddt", "ptm", "iptm")},
        esm_git_revision=result["esm_git_revision"],
        transformers_git_revision=result["transformers_git_revision"],
    )


def parse_modal_binder_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Modal binder result must be a JSON object")
    sequences = value.get("sequences")
    trajectory = value.get("trajectory")
    critic_results = value.get("critic_results")
    if not (
        isinstance(sequences, list)
        and sequences
        and all(
            isinstance(sequence, str) and sequence.count("|") == 1
            for sequence in sequences
        )
    ):
        raise ValidationError(
            "Modal binder result requires target|binder canonical sequence strings"
        )
    if not isinstance(trajectory, dict) or not trajectory:
        raise ValidationError("Modal binder result is missing its trajectory")
    if not isinstance(critic_results, list) or not critic_results:
        raise ValidationError("Modal binder result is missing critic results")
    try:
        final_losses = [float(result["final_loss"]) for result in critic_results]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Modal binder critic results lack numeric final_loss") from exc
    if any(not math.isfinite(loss) for loss in final_losses):
        raise ValidationError("Modal binder critic final_loss must be finite")

    def require_finite_numbers(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                require_finite_numbers(child)
        elif isinstance(item, list):
            for child in item:
                require_finite_numbers(child)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValidationError("Modal binder trajectory contains non-finite values")

    require_finite_numbers(trajectory)
    component_digests = []
    for sequence in sequences:
        target, binder = sequence.split("|")
        from biohub_esm_lib.validation import validate_protein_sequence

        validate_protein_sequence(target)
        validate_protein_sequence(binder)
        component_digests.append(
            {
                "complex_sha256": input_digest(sequence),
                "target_sha256": input_digest(target),
                "binder_sha256": input_digest(binder),
            }
        )
    return {
        "generated_sequences": sequences,
        "generated_sequences_sha256": input_digest(sequences),
        "component_digests": component_digests,
        "trajectory_steps": len(trajectory),
        "critic_result_count": len(critic_results),
        "average_final_loss": sum(final_losses) / len(final_losses),
    }


def run_modal_binder(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_cost:
        raise ValidationError("binder smoke requires --confirm-cost after reviewing the cost estimate")
    h100_gpu_upper_bound_usd = 3.9492
    volume_planning_ceiling_gib = 55.0
    if (
        not math.isfinite(args.max_gpu_cost_usd)
        or args.max_gpu_cost_usd <= 0
        or args.max_gpu_cost_usd < h100_gpu_upper_bound_usd
    ):
        raise ValidationError(
            "--max-gpu-cost-usd is below the one-hour H100 GPU upper-bound estimate"
        )
    if not args.confirm_volume_cleanup:
        raise ValidationError(
            "binder smoke requires --confirm-volume-cleanup for ticket-scoped Volumes"
        )
    root = Path(args.example_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    require_fresh_output_dir(output_dir, "Modal binder smoke")
    started = utc_now()
    with tempfile.TemporaryDirectory(prefix="biohub-esm-binder-") as temporary:
        prepared_root = Path(temporary)
        source_pins = prepare_official_binder_example(root, prepared_root)
        write_json_atomic(output_dir / "source-pins.json", source_pins)
        command = [
            args.modal_cli,
            "run",
            "-m",
            "06_gpu_and_ml.binder-design.esmfold2_binder_design::main",
            "--target-name",
            "pd-l1",
            "--binder-name",
            "minibinder",
            "--seed",
            "0",
            "--batch-size",
            "1",
            "--output-path",
            str(output_dir / "binder-result.json"),
        ]
        completed, cleanup = run_modal_command_with_cleanup(
            command,
            cwd=prepared_root,
            modal_cli=args.modal_cli,
            volume_names=BINDER_VOLUME_NAMES,
            timeout=70 * 60,
        )
    write_json_atomic(
        output_dir / "modal-log.json",
        redact(
            {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
                "volume_cleanup": cleanup,
            }
        ),
    )
    if not cleanup["all_succeeded"]:
        raise ValidationError(
            "Modal binder smoke could not confirm deletion of ticket-scoped Volumes"
        )
    if completed.returncode != 0:
        if "payment method" in (completed.stderr + completed.stdout).lower():
            raise ValidationError(
                "Modal binder GPU allocation is blocked because the workspace lacks a payment method"
            )
        raise ValidationError("single-seed Modal binder smoke failed; see redacted modal-log.json")
    binder_result_path = output_dir / "binder-result.json"
    if not binder_result_path.is_file():
        raise ValidationError("Modal binder smoke did not produce its structured result")
    binder_result = load_strict_result(binder_result_path, "Modal binder")
    result = {
        "target": "pd-l1",
        "binder_template": "minibinder",
        "n_seeds": 1,
        "representative": False,
        "claim": "integration smoke only; not representative screening depth",
        "h100_gpu_upper_bound_usd": h100_gpu_upper_bound_usd,
        "confirmed_gpu_cost_ceiling_usd": args.max_gpu_cost_usd,
        "modal_volumes": list(BINDER_VOLUME_NAMES),
        "volume_planning_ceiling_gib": volume_planning_ceiling_gib,
        "volume_cleanup_policy": "delete-after-smoke",
        "volume_deletion_billing_lag_days_max": 4,
        "source_pins": source_pins,
        **parse_modal_binder_result(binder_result),
    }
    return save_result(
        output_dir,
        result=result,
        route="modal",
        endpoint="modal://example-esmfold2-binder-design/BinderDesignService.design",
        model_id="biohub/ESMFold2-Experimental-Fast",
        model_revision=MODAL_BINDER_HF_REVISIONS[
            "biohub/ESMFold2-Experimental-Fast"
        ],
        inputs={"target_name": "pd-l1", "binder_name": "minibinder"},
        parameters={
            "n_seeds": 1,
            "batch_size": 1,
            "representative": False,
            "scaling_critics": False,
            "h100_gpu_upper_bound_usd": h100_gpu_upper_bound_usd,
            "confirmed_gpu_cost_ceiling_usd": args.max_gpu_cost_usd,
            "modal_volumes": list(BINDER_VOLUME_NAMES),
            "volume_planning_ceiling_gib": volume_planning_ceiling_gib,
            "volume_list_price_per_gib_month": MODAL_VOLUME_LIST_PRICE_PER_GIB_MONTH,
            "volume_retained_monthly_list_upper_bound_usd": (
                volume_planning_ceiling_gib
                * MODAL_VOLUME_LIST_PRICE_PER_GIB_MONTH
            ),
            "volume_cleanup_policy": "delete-after-smoke",
            "volume_deletion_billing_lag_days_max": 4,
            "model_revisions": MODAL_BINDER_HF_REVISIONS,
            "modal_examples_revision": source_pins["modal_examples_revision"],
        },
        seed=0,
        started_at=started,
        extra_artifacts=[
            artifact_record(output_dir / "modal-log.json", media_type="application/json"),
            artifact_record(output_dir / "source-pins.json", media_type="application/json"),
            artifact_record(binder_result_path, media_type="application/json"),
        ],
        esm_git_revision=MODAL_BINDER_ESM_GIT_REVISION,
        transformers_git_revision=TRANSFORMERS_GIT_REVISION,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="smoke", required=True)
    for name in ("atlas", "biohub-esmc", "biohub-esmfold2", "hf-esmc", "hf-esmfold2"):
        command = sub.add_parser(name)
        command.add_argument("--output-dir", required=True)
    modal_fold = sub.add_parser("modal-fold")
    modal_fold.add_argument("--output-dir", required=True)
    modal_fold.add_argument("--modal-cli", default="/home/dev-user/.venvs/modal/bin/modal")
    modal_fold.add_argument("--confirm-cost", action="store_true")
    modal_fold.add_argument("--confirm-volume-cleanup", action="store_true")
    modal_fold.add_argument("--max-gpu-cost-usd", type=float, required=True)
    modal_binder = sub.add_parser("modal-binder")
    modal_binder.add_argument("--output-dir", required=True)
    modal_binder.add_argument("--example-root", required=True)
    modal_binder.add_argument("--modal-cli", default="/home/dev-user/.venvs/modal/bin/modal")
    modal_binder.add_argument("--confirm-cost", action="store_true")
    modal_binder.add_argument("--confirm-volume-cleanup", action="store_true")
    modal_binder.add_argument("--max-gpu-cost-usd", type=float, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir).resolve()
    try:
        require_fresh_output_dir(output_dir, f"{args.smoke} smoke")
        if args.smoke == "atlas":
            result = smoke_atlas(output_dir)
        elif args.smoke == "biohub-esmc":
            result = smoke_biohub_esmc(output_dir)
        elif args.smoke == "biohub-esmfold2":
            result = smoke_biohub_esmfold2(output_dir)
        elif args.smoke == "hf-esmc":
            result = smoke_hf_esmc(output_dir)
        elif args.smoke == "hf-esmfold2":
            result = smoke_hf_esmfold2(output_dir)
        elif args.smoke == "modal-fold":
            result = run_modal_fold(args)
        else:
            result = run_modal_binder(args)
        print(json.dumps(redact(result), indent=2, sort_keys=True))
        return 0 if result.get("status") != "skipped" else 3
    except Exception as exc:
        # Live-provider exceptions can originate below our typed boundary. Keep
        # diagnostics traceback-free and recursively redact the environment.
        error = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
        print(json.dumps(redact(error), indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
