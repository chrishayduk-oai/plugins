# ESM Atlas v1alpha1 HTTP contract

Primary sources: [overview](https://biohub.ai/esm/protein/atlas/api-docs/overview.html)
and [API reference](https://biohub.ai/esm/protein/atlas/api-docs/api_reference.html).
Verified live 2026-07-01. Base URL: `https://biohub.ai`; no client-side auth.

| Method and path | Purpose |
| --- | --- |
| `GET /esm/protein/api/v1alpha1/features` | all 16,384 feature summaries |
| `GET /esm/protein/api/v1alpha1/features/{index}` | detailed feature metadata |
| `GET /esm/protein/api/v1alpha1/proteins/{md5}` | sequence, metadata, structure, confidence, SAE features, cluster representative |
| `GET /esm/protein/api/v1alpha1/proteins/{md5}/thumbnail/{type}` | `plddt` or `pct-characterized` PNG |
| `GET /esm/protein/api/v1alpha1/clusters/{representative-md5}` | members, Pfam, taxonomy, SAE context |
| `GET /esm/protein/api/v1alpha1/similarity-search` | SAE-vector search from sequence |
| `POST /esm/protein/api/v1alpha1/proteins/batch` | immediate zip or async job |
| `GET /esm/protein/api/v1alpha1/proteins/batch/jobs/{job_id}` | pending/completed/cancelled/failed/expired state |
| `DELETE /esm/protein/api/v1alpha1/proteins/batch/jobs/{job_id}` | idempotent cancellation request |

## Search query

Required `sequence`; optional `topk_results` 1–100, `topk_features` 1–100,
`min_similarity` 0–1, `cluster_pct_characterized_max` 0–100, and
`include_cluster_info`. The live schema advertises sequence length up to 2,048,
but release documentation has conflicted; this plugin conservatively enforces
800 until the conflict is resolved.

Response requires `query_sequence` and `similar_proteins`; it may include the
query MD5 when present in Atlas, shared feature statistics, and
`restricted_count`. Each hit includes MD5, accession, length, cosine similarity,
and may include PDB/pTM/pLDDT plus cluster fields. An empty hit list is valid.

## Protein and cluster

Protein query parameters: `topk_features` 1–100, `fold_on_miss`,
`normalize_features`, and repeated `feature_indices` capped at 100. An on-demand
fold may return `folded_on_demand=true`; enforce 699 residues conservatively.
Use `cluster_rep_protein_hash` from the protein response before calling the
cluster endpoint. The cluster endpoint is defined for a representative hash.

This is alpha. Unknown status values, missing required keys, non-JSON bodies, or
field type changes are schema drift. Save the raw response and fail clearly
rather than guessing.
