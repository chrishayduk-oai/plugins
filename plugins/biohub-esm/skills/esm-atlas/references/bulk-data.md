# Atlas batch and anonymous S3

## Batch lifecycle

The request accepts at most 500 unique MD5 hashes and flags for sequence,
structure, cluster info, and protein/per-residue features. Small requests may
return a zip with HTTP 200. Larger requests return HTTP 202 with `job_id` and
polling state.

Persist `job_id`, status, counts, and timestamps. Treat these as terminal:
`completed`, `cancelled`, `failed`, `expired`. A cancel returns 204 idempotently;
if output was already produced, later GET may still return it. Signed download
URLs are ephemeral capabilities: keep one in memory only, never durable state,
and reacquire it by polling when resuming. Use `.partial` plus HTTPS Range
resume when supported, then validate the complete ZIP, atomically rename, and
SHA-256 checksum.

## Anonymous data

The [Biohub get-started guide](https://biohub.ai/esm/protein/get-started)
publishes data under `s3://esm-protein-atlas/v1/` with CC-BY-4.0 licensing.

```bash
aws s3 sync --no-sign-request \
  s3://esm-protein-atlas/v1/clusters/indexes/secondary/cluster_members/ \
  /absolute/path/atlas-cluster-members
```

Use the narrowest dataset prefix. Approximate published sizes are large:
sequences ~2.2 TB, structures ~68.9 TB, SAE feature data ~306 TB, and the full
release ~377 TB. Cluster membership (~26 GB) is the recommended manageable
starting point. Show expected transfer/storage cost and obtain confirmation
before a large sync. Record bucket prefix, ETag/checksum where available,
download timestamp, and license attribution.
