"""Bounded official-pattern Modal ESMFold2-Fast integration smoke."""

from pathlib import Path

import modal

ESM_REVISION = "ba4d7124864eed323a93bf3cfefcd958f573b75a"
TRANSFORMERS_REVISION = "ef32577f55da19a4989cd7b22e004dc43a4998cb"
MODEL_REPO = "biohub/ESMFold2-Fast"
MODEL_REVISION = "b28d8ace5e05e61e5bec1e6820cfd3e221819d12"
SEQUENCE = "MKTAYIAKQRQISFVKSHFSRQ"
MODEL_VOLUME_NAME = "lsc110-biohub-esm-fold-models-v1"


def _optional_float(value):
    return None if value is None else float(value)


def _verified_vcs_revision(distribution_name: str, expected: str) -> str:
    import importlib.metadata as metadata
    import json

    distribution = metadata.distribution(distribution_name)
    direct_url = json.loads(distribution.read_text("direct_url.json") or "null")
    vcs_info = direct_url.get("vcs_info") if isinstance(direct_url, dict) else None
    if not isinstance(vcs_info, dict) or (
        vcs_info.get("commit_id") != expected
        or vcs_info.get("requested_revision") != expected
    ):
        raise RuntimeError(
            f"{distribution_name} installed/requested revision does not match its pin"
        )
    return expected

app = modal.App("biohub-esm-esmfold2-smoke")
models = modal.Volume.from_name(MODEL_VOLUME_NAME, create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .uv_pip_install(
        f"esm @ git+https://github.com/Biohub/esm.git@{ESM_REVISION}",
    )
    .uv_pip_install(
        f"transformers @ git+https://github.com/Biohub/transformers.git@{TRANSFORMERS_REVISION}",
        extra_options="--force-reinstall --no-deps",
    )
    .env(
        {
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
            "HF_HOME": "/models",
            "HF_XET_HIGH_PERFORMANCE": "1",
        }
    )
)


@app.cls(image=image, volumes={"/models": models}, gpu="H100", timeout=20 * 60)
class ESMFold2Smoke:
    @modal.enter()
    def load(self):
        from esm.models.esmfold2 import ESMFold2InputBuilder
        from transformers.models.esmfold2.modeling_esmfold2 import ESMFold2Model

        self.code_revisions = {
            "esm": _verified_vcs_revision("esm", ESM_REVISION),
            "transformers": _verified_vcs_revision(
                "transformers", TRANSFORMERS_REVISION
            ),
        }
        self.input_builder = ESMFold2InputBuilder()
        self.model = ESMFold2Model.from_pretrained(
            MODEL_REPO, revision=MODEL_REVISION
        ).cuda().eval()

    @modal.method()
    def fold(self, sequence: str):
        import torch

        from esm.models.esmfold2 import ProteinInput, StructurePredictionInput

        torch.manual_seed(0)
        torch.cuda.manual_seed_all(0)
        torch.use_deterministic_algorithms(True)
        fold_input = StructurePredictionInput(
            sequences=[ProteinInput(id="A", sequence=sequence)]
        )
        output = self.input_builder.fold(
            self.model,
            fold_input,
            num_loops=3,
            num_sampling_steps=20,
            seed=0,
            lm_dropout=0.0,
        )
        return {
            "mmcif": output.complex.to_mmcif(),
            "mean_plddt": float(output.plddt.float().mean()),
            "ptm": _optional_float(output.ptm),
            "iptm": _optional_float(output.iptm),
            "model_id": MODEL_REPO,
            "model_revision": MODEL_REVISION,
            "esm_git_revision": self.code_revisions["esm"],
            "transformers_git_revision": self.code_revisions["transformers"],
            "seed": 0,
        }


@app.local_entrypoint()
def main(output_dir: str):
    import hashlib
    import json

    output = ESMFold2Smoke().fold.remote(SEQUENCE)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "prediction.cif").write_text(output.pop("mmcif"), encoding="utf-8")
    output["input_sha256"] = hashlib.sha256(SEQUENCE.encode()).hexdigest()
    (root / "modal-result.json").write_text(
        json.dumps(output, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
