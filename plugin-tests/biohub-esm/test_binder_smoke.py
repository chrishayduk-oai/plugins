from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib import binder_smoke
from biohub_esm_lib.constants import MODAL_BINDER_HF_REVISIONS, TRANSFORMERS_GIT_REVISION
from biohub_esm_lib.errors import ValidationError


ENTRYPOINT_FIXTURE = '''from pathlib import Path
ESM_REVISION = (
    "f652b471d29da828b31e9b7a9cf7d0a7803240f5"
)

models_volume = modal.Volume.from_name("esmfold2-models", create_if_missing=True)
results_volume = modal.Volume.from_name(
    "esmfold2-binder-design-results", create_if_missing=True
)

image = (
    modal.Image.micromamba()
    .uv_pip_install(
        f"esm @ git+https://github.com/Biohub/esm.git@{ESM_REVISION}",
        "abnumber==0.4.4",
        "pyarrow==18.1.0",
    )
    .env({})
)

@app.local_entrypoint()
def main(
    target_name: Optional[str] = "pd-l1",
    use_scaling_critics: bool = False,
    seed: int = 0,
    batch_size: int = 1,
):
    designer = BinderDesignService(use_scaling_critics=use_scaling_critics)
    seq, trajectory, results = designer.design.remote(
        target_name=target_name,
        seed=seed,
        batch_size=batch_size,
    )

    avg_final_loss = sum(r["final_loss"] for r in results) / len(results)
'''

MODELS_FIXTURE = '''from typing import Any
_ESMC = None
class ESMFold2Designer:
    def load(self, use_scaling_critics: bool):
        if use_scaling_critics:
            pass
model = ESMFold2ExperimentalModel.from_pretrained(repo_id, load_esmc=not cache_esmc)
if cache_esmc:
    if _ESMC is None:
            model.load_esmc(model.config.esmc_id)
        self.esmc_model = ESMCForMaskedLM.from_pretrained(
            self.lm_name, torch_dtype=torch.float32
        )
'''


class BinderSourceTests(unittest.TestCase):
    def test_patch_injects_exact_dependency_and_model_revisions(self) -> None:
        entrypoint = binder_smoke.patch_binder_entrypoint(ENTRYPOINT_FIXTURE)
        models = binder_smoke.patch_binder_models(MODELS_FIXTURE)
        self.assertIn(TRANSFORMERS_GIT_REVISION, entrypoint)
        self.assertIn('extra_options="--force-reinstall --no-deps"', entrypoint)
        self.assertIn('"binder-smoke-result.json"', entrypoint)
        self.assertIn('"critic_results": results', entrypoint)
        for repo, revision in MODAL_BINDER_HF_REVISIONS.items():
            self.assertIn(repo, models)
            self.assertIn(revision, models)
        self.assertIn("load_esmc=False", models)
        self.assertIn("_load_pinned_esmc(model)", models)
        self.assertIn('vcs_info.get("requested_revision")', models)
        self.assertIn("_verify_code_revision(name)", models)

    def test_prepare_rejects_unreviewed_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
            completed = subprocess.CompletedProcess([], 0, "bad-revision\n", "")
            with patch("biohub_esm_lib.binder_smoke.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(ValidationError, "reviewed binder revision"):
                    binder_smoke.prepare_official_binder_example(
                        Path(source), Path(dest)
                    )

    @staticmethod
    def archive_fixture() -> bytes:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for relative, content in (
                (
                    "06_gpu_and_ml/binder-design/esmfold2_binder_design.py",
                    ENTRYPOINT_FIXTURE.encode(),
                ),
                (
                    "06_gpu_and_ml/binder-design/binder_design/models.py",
                    MODELS_FIXTURE.encode(),
                ),
            ):
                info = tarfile.TarInfo(relative)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return payload.getvalue()

    def test_prepare_verifies_hashes_and_records_patched_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
            root = Path(source)
            entry = root / "06_gpu_and_ml/binder-design/esmfold2_binder_design.py"
            models = root / "06_gpu_and_ml/binder-design/binder_design/models.py"
            hashes = {
                str(entry.relative_to(root)): hashlib.sha256(ENTRYPOINT_FIXTURE.encode()).hexdigest(),
                str(models.relative_to(root)): hashlib.sha256(MODELS_FIXTURE.encode()).hexdigest(),
            }
            revision = subprocess.CompletedProcess(
                [], 0, f"{binder_smoke.MODAL_BINDER_EXAMPLE_REVISION}\n", ""
            )
            archived = subprocess.CompletedProcess([], 0, self.archive_fixture(), b"")
            with (
                patch(
                    "biohub_esm_lib.binder_smoke.subprocess.run",
                    side_effect=[revision, archived],
                ),
                patch("biohub_esm_lib.binder_smoke.MODAL_BINDER_SOURCE_SHA256", hashes),
            ):
                result = binder_smoke.prepare_official_binder_example(root, Path(dest))
            self.assertEqual(result["upstream_source_sha256"], hashes)
            self.assertEqual(len(result["patched_source_sha256"]), 2)
            self.assertEqual(len(result["archived_subtree_sha256"]), 64)
            self.assertFalse(result["scaling_critics"])


if __name__ == "__main__":
    unittest.main()
