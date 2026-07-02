"""Verify and revision-pin the official Modal binder example for a bounded smoke."""

from __future__ import annotations

import json
import io
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from .constants import (
    MODAL_BINDER_ESM_GIT_REVISION,
    MODAL_BINDER_EXAMPLE_REVISION,
    MODAL_BINDER_HF_REVISIONS,
    MODAL_BINDER_SOURCE_SHA256,
    TRANSFORMERS_GIT_REVISION,
)
from .errors import ValidationError
from .provenance import sha256_bytes, sha256_file

BINDER_RELATIVE_ROOT = Path("06_gpu_and_ml/binder-design")
BINDER_VOLUME_NAMES = (
    "lsc110-biohub-esm-binder-models-v1",
    "lsc110-biohub-esm-binder-results-v1",
)


def _replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise ValidationError(f"official Modal binder source changed at {label}")
    return value.replace(old, new, 1)


def patch_binder_entrypoint(value: str) -> str:
    value = _replace_once(
        value,
        "from pathlib import Path",
        "import json\nfrom pathlib import Path",
        "structured-result import",
    )
    value = _replace_once(
        value,
        ")\n\nimage = (",
        f')\nTRANSFORMERS_REVISION = "{TRANSFORMERS_GIT_REVISION}"\n\nimage = (',
        "image constants",
    )
    value = _replace_once(
        value,
        '"esmfold2-models"',
        f'"{BINDER_VOLUME_NAMES[0]}"',
        "ticket-scoped model volume",
    )
    value = _replace_once(
        value,
        '"esmfold2-binder-design-results"',
        f'"{BINDER_VOLUME_NAMES[1]}"',
        "ticket-scoped results volume",
    )
    value = _replace_once(
        value,
        '        "pyarrow==18.1.0",\n'
        "    )\n"
        "    .env(",
        '        "pyarrow==18.1.0",\n'
        "    )\n"
        "    .uv_pip_install(\n"
        "        f\"transformers @ git+https://github.com/Biohub/transformers.git@{TRANSFORMERS_REVISION}\",\n"
        '        extra_options="--force-reinstall --no-deps",\n'
        "    )\n"
        "    .env(",
        "pinned Transformers install",
    )
    value = _replace_once(
        value,
        "    batch_size: int = 1,\n"
        "):\n"
        "    designer = BinderDesignService(use_scaling_critics=use_scaling_critics)",
        "    batch_size: int = 1,\n"
        '    output_path: str = "binder-smoke-result.json",\n'
        "):\n"
        "    designer = BinderDesignService(use_scaling_critics=use_scaling_critics)",
        "structured-result argument",
    )
    return _replace_once(
        value,
        "        batch_size=batch_size,\n"
        "    )\n\n"
        "    avg_final_loss =",
        "        batch_size=batch_size,\n"
        "    )\n\n"
        "    Path(output_path).write_text(\n"
        "        json.dumps(\n"
        "            {\n"
        '                "sequences": seq,\n'
        '                "trajectory": trajectory,\n'
        '                "critic_results": results,\n'
        "            },\n"
        "            indent=2,\n"
        "            sort_keys=True,\n"
        "        )\n"
        '        + "\\n",\n'
        '        encoding="utf-8",\n'
        "    )\n\n"
        "    avg_final_loss =",
        "structured-result write",
    )


def patch_binder_models(value: str) -> str:
    revisions = json.dumps(MODAL_BINDER_HF_REVISIONS, indent=4, sort_keys=True)
    code_revisions = json.dumps(
        {
            "esm": MODAL_BINDER_ESM_GIT_REVISION,
            "transformers": TRANSFORMERS_GIT_REVISION,
        },
        indent=4,
        sort_keys=True,
    )
    block = (
        "_ESMC = None\n\n"
        f"MODEL_REVISIONS = {revisions}\n"
        f"CODE_REVISIONS = {code_revisions}\n\n\n"
        "def _verify_code_revision(distribution_name: str) -> str:\n"
        "    import importlib.metadata as metadata\n"
        "    import json\n\n"
        "    expected = CODE_REVISIONS[distribution_name]\n"
        "    distribution = metadata.distribution(distribution_name)\n"
        "    direct_url = json.loads(distribution.read_text(\"direct_url.json\") or \"null\")\n"
        "    vcs_info = direct_url.get(\"vcs_info\") if isinstance(direct_url, dict) else None\n"
        "    if not isinstance(vcs_info, dict) or (\n"
        "        vcs_info.get(\"commit_id\") != expected\n"
        "        or vcs_info.get(\"requested_revision\") != expected\n"
        "    ):\n"
        "        raise RuntimeError(\n"
        "            f\"{distribution_name} installed/requested revision does not match its pin\"\n"
        "        )\n"
        "    return expected\n\n\n"
        "def _load_pinned_esmc(model: Any) -> None:\n"
        "    from transformers.models.esmc.modeling_esmc import ESMCModel\n\n"
        "    repo_id = model.config.esmc_id\n"
        "    esmc = ESMCModel.from_pretrained(\n"
        "        repo_id, revision=MODEL_REVISIONS[repo_id]\n"
        "    )\n"
        "    model._esmc = esmc.bfloat16().to(model.device).eval()\n"
    )
    value = _replace_once(value, "_ESMC = None", block.rstrip(), "model revision map")
    value = _replace_once(
        value,
        "    def load(self, use_scaling_critics: bool):\n"
        "        if use_scaling_critics:",
        "    def load(self, use_scaling_critics: bool):\n"
        "        self.code_revisions = {\n"
        "            name: _verify_code_revision(name) for name in CODE_REVISIONS\n"
        "        }\n"
        "        if use_scaling_critics:",
        "runtime code revision verification",
    )
    value = _replace_once(
        value,
        "model = ESMFold2ExperimentalModel.from_pretrained(repo_id, load_esmc=not cache_esmc)",
        "model = ESMFold2ExperimentalModel.from_pretrained(\n"
        "        repo_id, revision=MODEL_REVISIONS[repo_id], load_esmc=False\n"
        "    )\n"
        "    if not cache_esmc:\n"
        "        _load_pinned_esmc(model)",
        "experimental checkpoint load",
    )
    value = _replace_once(
        value,
        "            model.load_esmc(model.config.esmc_id)",
        "            _load_pinned_esmc(model)",
        "shared ESMC load",
    )
    return _replace_once(
        value,
        "        self.esmc_model = ESMCForMaskedLM.from_pretrained(\n"
        "            self.lm_name, torch_dtype=torch.float32\n"
        "        )",
        "        self.esmc_model = ESMCForMaskedLM.from_pretrained(\n"
        "            self.lm_name,\n"
        "            revision=MODEL_REVISIONS[self.lm_name],\n"
        "            torch_dtype=torch.float32,\n"
        "        )",
        "ESMC masked-LM load",
    )


def prepare_official_binder_example(example_root: Path, destination_root: Path) -> dict[str, Any]:
    """Verify exact upstream bytes, copy the example, and inject immutable dependency pins."""

    example_root = example_root.resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=example_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if revision.returncode != 0 or revision.stdout.strip() != MODAL_BINDER_EXAMPLE_REVISION:
        raise ValidationError("Modal examples checkout is not at the reviewed binder revision")

    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            MODAL_BINDER_EXAMPLE_REVISION,
            "--",
            str(BINDER_RELATIVE_ROOT),
        ],
        cwd=example_root,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0 or not isinstance(archive.stdout, bytes) or not archive.stdout:
        raise ValidationError("could not archive the reviewed Modal binder source subtree")
    destination_root.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            bundle.extractall(destination_root, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ValidationError("reviewed Modal binder source archive is invalid") from exc

    upstream: dict[str, str] = {}
    for relative, expected in MODAL_BINDER_SOURCE_SHA256.items():
        path = destination_root / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise ValidationError(f"official Modal binder source hash mismatch: {relative}")
        upstream[relative] = expected

    destination = destination_root / BINDER_RELATIVE_ROOT

    entrypoint = destination / "esmfold2_binder_design.py"
    models = destination / "binder_design/models.py"
    entrypoint.write_text(
        patch_binder_entrypoint(entrypoint.read_text(encoding="utf-8")), encoding="utf-8"
    )
    models.write_text(patch_binder_models(models.read_text(encoding="utf-8")), encoding="utf-8")
    patched = {
        str(path.relative_to(destination_root)): sha256_file(path)
        for path in (entrypoint, models)
    }
    return {
        "modal_examples_revision": MODAL_BINDER_EXAMPLE_REVISION,
        "archived_subtree_sha256": sha256_bytes(archive.stdout),
        "upstream_source_sha256": upstream,
        "patched_source_sha256": patched,
        "esm_git_revision": MODAL_BINDER_ESM_GIT_REVISION,
        "transformers_git_revision": TRANSFORMERS_GIT_REVISION,
        "model_revisions": MODAL_BINDER_HF_REVISIONS,
        "scaling_critics": False,
        "modal_volumes": list(BINDER_VOLUME_NAMES),
        "volume_cleanup_policy": "delete-after-smoke",
    }
