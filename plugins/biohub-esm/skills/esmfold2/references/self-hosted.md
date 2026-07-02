# ESMFold2 on Modal or user-owned compute

The [official Modal ESMFold2 example](https://modal.com/docs/examples/esmfold2)
shows a pinned `Biohub/esm` install, Hugging Face revision, H100 function,
persistent model Volume, and mmCIF output. Treat it as a starting point and
update its pins to the plugin's validated
[`source-pins.md`](../../../references/source-pins.md).

## Route

- Modal: many independent folds, parameter sweeps, durable/long jobs, or burst
  capacity. Requires Modal auth but not `ESM_API_KEY` for public weights.
- Self-hosted: privacy, offline/data-resident requirements, custom code, owned
  GPUs, fine-tuning, or sustained utilization. `HF_TOKEN` is optional for public
  weights.

## Local full-model skeleton

Install both code repositories from the exact revisions in `source-pins.md`;
the ESM package's upstream dependency currently names a mutable Transformers
branch, so pinning only ESM is insufficient.

```python
from transformers.models.esmfold2.modeling_esmfold2 import ESMFold2Model

repo = "biohub/ESMFold2"
revision = "1ebf0e3481a5184eb6171d40615c79e384b48796"
model = ESMFold2Model.from_pretrained(repo, revision=revision).cuda().eval()
```

Use `biohub/ESMFold2-Fast` revision
`b28d8ace5e05e61e5bec1e6820cfd3e221819d12` for fast single-sequence
throughput. Verify CUDA/ROCm compatibility, model dtype, deterministic settings,
and output checksums. Never silently fall back to CPU, a different revision, or
reduced parameters and then compare results as equivalent.

For hundreds of calls, use Modal map/spawn with bounded concurrency and
`return_exceptions=True` semantics. Persist call IDs and per-input provenance so
partial successes survive client interruption.

## Bounded Modal smoke

At the [H100 price](https://modal.com/pricing) verified on 2026-07-01
(`$0.001097/second`), the smoke's 20-minute GPU timeout has a GPU-only upper
bound of `$1.3164`; CPU, memory, Volume, and transfer charges are additional.
Refresh pricing and explicitly confirm that bound before running:

```bash
python3 <repo-root>/plugin-tests/biohub-esm/live_smoke.py modal-fold \
  --output-dir /absolute/path/to/evidence \
  --max-gpu-cost-usd 1.3164 --confirm-cost --confirm-volume-cleanup
```

The numeric argument records the confirmed ceiling; the Modal function timeout,
not the argument itself, enforces GPU duration.

The smoke uses ticket-scoped Volume `lsc110-biohub-esm-fold-models-v1`, plans
for at most 20 GiB, and deletes it (with `--allow-missing`) after every outcome.
At the 2026-07-01 list price of `$0.09/GiB/month` (with 1 TiB/month included),
retaining that planning ceiling would list at `$1.80/month`; the harness does
not intentionally retain it. Per [Modal's Volume pricing
documentation](https://modal.com/docs/guide/volumes#pricing), usage is
snapshotted daily, so storage deleted by the cleanup can still be billed for up
to four days. Cleanup limits retention; it does not promise zero post-delete
storage cost.
