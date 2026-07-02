"""Explicit scientific and compute routing decision table."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .constants import MODAL_BINDER_EXAMPLE_REVISION
from .errors import ValidationError

Task = Literal["atlas", "esmc", "fold", "binder-design"]
Route = Literal["atlas-api", "atlas-s3", "biohub", "modal", "self-hosted"]


@dataclass(frozen=True)
class RouteRequest:
    task: Task
    item_count: int = 1
    long_running: bool = False
    bulk_dataset: bool = False
    private: bool = False
    offline: bool = False
    data_residency: bool = False
    custom_model: bool = False
    fine_tune: bool = False
    sustained_workload: bool = False
    has_msa: bool = False
    accuracy_priority: bool = False
    owns_gpu: bool = False


@dataclass(frozen=True)
class RouteResult:
    route: Route
    model: str | None
    credential: str | None
    rationale: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    workflow: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def route_request(request: RouteRequest) -> RouteResult:
    if request.task not in {"atlas", "esmc", "fold", "binder-design"}:
        raise ValidationError(f"unsupported task: {request.task}")
    if request.item_count < 1:
        raise ValidationError("item_count must be at least 1")

    private_execution = any(
        (
            request.private,
            request.offline,
            request.data_residency,
            request.custom_model,
            request.fine_tune,
            request.sustained_workload,
        )
    )

    if request.task == "atlas":
        if private_execution or request.bulk_dataset:
            rationale = (
                "Private/offline Atlas work must stage the public dataset locally; do not send "
                "a private query to the public API."
                if private_execution
                else "Atlas bulk datasets are public through anonymous S3."
            )
            return RouteResult(
                route="atlas-s3",
                model=None,
                credential=None,
                rationale=(rationale,),
                warnings=(
                    "Atlas data is CC-BY-4.0; preserve attribution and dataset provenance.",
                    "Atlas data is very large; scope transfer, storage, and local search costs first.",
                ),
            )
        return RouteResult(
            route="atlas-api",
            model=None,
            credential=None,
            rationale=("Protein discovery, SAE features, and cluster exploration belong to Atlas.",),
            warnings=("Atlas is a v1alpha1 API; validate schemas and preserve raw responses.",),
        )

    if request.task == "binder-design":
        if private_execution or request.owns_gpu:
            return RouteResult(
                route="self-hosted",
                model=None,
                credential=None,
                rationale=(
                    "Binder design is not available through the Biohub managed API.",
                    "Private/custom execution or owned GPUs favor pinned open weights.",
                    "The workflow uses pinned experimental inversion and critic checkpoints, "
                    "not the standard ESMFold2 inference checkpoint.",
                ),
                warnings=(
                    "A one-seed smoke test is not representative; useful campaigns "
                    "usually need hundreds and often about 1,000 designs.",
                    "Estimate runtime and cost, then obtain explicit confirmation before material GPU spend.",
                ),
                workflow=f"modal-esmfold2-binder-design@{MODAL_BINDER_EXAMPLE_REVISION}",
            )
        return RouteResult(
            route="modal",
            model=None,
            credential="MODAL_TOKEN_ID + MODAL_TOKEN_SECRET or Modal profile",
            rationale=(
                "Binder design is not available through the Biohub managed API.",
                "Modal supports parallel, durable execution of the released open-weight workflow.",
                "The workflow uses pinned experimental inversion and critic checkpoints, "
                "not the standard ESMFold2 inference checkpoint.",
            ),
            warnings=(
                "A one-seed smoke test is not representative; useful campaigns "
                "usually need hundreds and often about 1,000 designs.",
                "Estimate runtime and cost, then obtain explicit confirmation before material GPU spend.",
            ),
            workflow=f"modal-esmfold2-binder-design@{MODAL_BINDER_EXAMPLE_REVISION}",
        )

    if private_execution:
        hf_model = "biohub/ESMC-600M" if request.task == "esmc" else "biohub/ESMFold2"
        return RouteResult(
            route="self-hosted",
            model=hf_model,
            credential=None,
            rationale=(
                "Private, offline, data-resident, customized, fine-tuned, or "
                "sustained work belongs on user-owned compute.",
                "Public weights do not require HF_TOKEN; it is optional for authenticated Hub access.",
            ),
        )

    # This is an orchestration heuristic, not an account quota. Account credits
    # and rate limits remain specific to the Biohub developer console.
    scale_out = request.long_running or request.item_count > 32
    if scale_out:
        model = "biohub/ESMC-600M" if request.task == "esmc" else (
            "biohub/ESMFold2" if request.accuracy_priority or request.has_msa else "biohub/ESMFold2-Fast"
        )
        return RouteResult(
            route="modal",
            model=model,
            credential="MODAL_TOKEN_ID + MODAL_TOKEN_SECRET or Modal profile",
            rationale=(
                "Independent bulk, parallel, or long-running inference should use durable scale-out compute.",
                "Public weights on Modal do not require ESM_API_KEY.",
            ),
        )

    if request.task == "esmc":
        return RouteResult(
            route="biohub",
            model="esmc-600m-2024-12",
            credential="ESM_API_KEY",
            rationale=("Biohub managed ESMC is the default for modest interactive inference.",),
        )

    model = "esmfold2-2026-05" if request.accuracy_priority or request.has_msa else "esmfold2-fast-2026-05"
    rationale = ["Biohub managed ESMFold2 is the default for modest interactive folding."]
    if request.has_msa:
        rationale.append("MSA conditioning requires the full ESMFold2 model, not Fast.")
    elif request.accuracy_priority:
        rationale.append("Accuracy-priority targets use full ESMFold2; Fast favors throughput.")
    else:
        rationale.append("ESMFold2-Fast favors single-sequence latency and throughput.")
    return RouteResult(
        route="biohub",
        model=model,
        credential="ESM_API_KEY",
        rationale=tuple(rationale),
        warnings=("Predicted structures are static hypotheses, not experimental truth or molecular dynamics.",),
    )
