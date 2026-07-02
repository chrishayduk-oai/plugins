---
name: esm-atlas
description: Use when the user needs ESM Atlas similarity search, MD5 protein lookup, structures, clusters, Pfam/taxonomy context, the 16,384 SAE features, thumbnails, batch jobs, or anonymous S3 data. Atlas is not a deployable model and currently needs no client key.
---

# ESM Atlas

Atlas is public data/discovery infrastructure, not another model to run through
Modal or Hugging Face. The current `v1alpha1` API needs no client-side key and is
explicitly unstable/not recommended for production assumptions.

## Conservative boundaries

- similarity search: validate at no more than 800 residues even though the live
  OpenAPI schema currently advertises a larger value
- on-demand fold: validate at no more than 699 residues (`<700` in the overview)
- feature index: 0–16,383
- batch: at most 500 unique MD5 hashes

Only relax a boundary after a live schema/probe is captured and tests are
updated. Preserve the raw alpha response before normalization.

## Workflows

```bash
# Learned-feature similarity search
python3 <plugin-root>/scripts/biohub_esm.py atlas search \
  --sequence "$SEQUENCE" --topk-results 10 --include-cluster-info \
  --output-dir /absolute/path/atlas-search

# Protein and then representative cluster traversal
python3 <plugin-root>/scripts/biohub_esm.py atlas protein \
  --protein-hash <md5> --output-dir /absolute/path/protein
python3 <plugin-root>/scripts/biohub_esm.py atlas cluster \
  --protein-hash <cluster-representative-md5> --output-dir /absolute/path/cluster

# Feature catalog/detail with raw response and provenance
python3 <plugin-root>/scripts/biohub_esm.py atlas features \
  --output-dir /absolute/path/features
python3 <plugin-root>/scripts/biohub_esm.py atlas feature \
  --feature-index 42 --output-dir /absolute/path/feature-42

# Batch submit creates atomic resumable state. --output handles a possible
# synchronous zip; the same path can be supplied again when waiting.
python3 <plugin-root>/scripts/biohub_esm.py atlas batch-submit \
  --hashes /absolute/path/hashes.json \
  --state /absolute/path/batch-state.json \
  --output /absolute/path/batch.zip
python3 <plugin-root>/scripts/biohub_esm.py atlas batch-status \
  --state /absolute/path/batch-state.json
python3 <plugin-root>/scripts/biohub_esm.py atlas batch-wait \
  --state /absolute/path/batch-state.json \
  --output /absolute/path/batch.zip
# Or request cancellation; terminal states are preserved as no-ops.
python3 <plugin-root>/scripts/biohub_esm.py atlas batch-cancel \
  --state /absolute/path/batch-state.json
```

Support feature catalog/list/detail, `pct-characterized` and `plddt`
thumbnails, batch submit/poll/cancel/download, and empty-hit results. Small
batches may return a zip immediately; large batches return `202` job state.
Every submit/poll/cancel transition atomically refreshes state and provenance,
so a new process can resume using only `--state`. Ephemeral signed download
URLs are used in memory and deliberately omitted from state. To adopt an older
job once, pass both `--state <new-path>` and `--job-id <id>`. Cancellation is
idempotent but completed results can remain available.

Use `cluster_pct_characterized_max=0` only as the documented proxy for clusters
without characterized Pfam annotations; do not call that proof of unknown
function. Preserve Atlas CC-BY-4.0 attribution.

Read the exact [alpha HTTP contract](references/api.md),
[batch and S3 guidance](references/bulk-data.md), and shared
[safety/provenance contract](../../references/safety-and-provenance.md).
