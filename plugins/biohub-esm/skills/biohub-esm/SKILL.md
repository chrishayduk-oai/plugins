---
name: biohub-esm
description: Route Biohub ESM protein requests to ESMC, ESMFold2, ESM Atlas, Modal, or private open weights. Use when the user asks broadly about Biohub ESM, protein representations or mutation scoring, biomolecular folding, Atlas discovery, binder design, or choosing compute. Not for unrelated sequence alignment or non-ESM models.
---

# Biohub ESM router

Use this as the implicit entry point. Identify the scientific goal before
choosing a model or provider; ESMC, ESMFold2, and Atlas are different artifacts.

## Route first

| User need | Default | Escalation |
| --- | --- | --- |
| Existing-protein discovery, functional neighborhoods, SAE features, clusters | public Atlas alpha API | anonymous Atlas S3 for bulk data |
| One or modest interactive ESMC calls | Biohub managed API | Modal for parallel/long jobs; self-host for private/custom work |
| One or modest structure predictions | Biohub managed ESMFold2 | full + MSA for difficult targets; Modal for bulk |
| Many independent folds or sweeps | Modal open weights | user-owned GPUs |
| Minibinder, binder, or scFv design | Modal or self-hosted open weights | never Biohub managed API until documented |
| Private, offline, air-gapped, data-resident, customized, fine-tuned, sustained | Hugging Face weights on user-owned compute | user owns capacity and operations |

Run the deterministic router when the route is not already explicit:

```bash
python3 <plugin-root>/scripts/biohub_esm.py route --task fold --item-count 500
```

The 32-item scale-out threshold is a planning heuristic, not a Biohub account
quota. Do not hardcode credits or rate limits; the developer console is the
account-specific source of truth.

## Hand off

After choosing the route, explicitly load exactly the focused specialist(s)
needed for the request. These specialists are explicit-only so the router stays
the single implicit entry point.

- Representation, logits, entropy, mutation, SAE, fitted-head, or fine-tuning
  requests: use `$esmc`.
- Protein/DNA/RNA/modified-residue/ligand folding: use `$esmfold2`.
- Similar proteins, MD5 records, clusters, feature catalog, thumbnails, or
  Atlas batch data: use `$esm-atlas`.
- Binder/minibinder/scFv design: use `$esmfold2-binder-design`.
- Install, authentication, environment, or preflight problems: use
  `$biohub-esm-setup`.

## Invariants

- Atlas is a public data/discovery API and anonymous dataset, not a model to
  deploy on Modal or Hugging Face.
- Biohub managed inference needs `ESM_API_KEY`.
- Modal public-weight workflows need Modal authentication, not `ESM_API_KEY`.
- Public Hugging Face weights do not require `HF_TOKEN`; it is optional for
  authenticated Hub access.
- Do not ask for credentials in chat or print, persist, screenshot, or commit
  them. Preflight reports only configured/missing.
- Before material GPU spend, show scope/runtime/cost evidence and obtain
  explicit confirmation.
- Preserve machine-readable artifacts and provenance, not UI-only results.

Read [routing details](references/routing.md), [failure handling](references/failures.md),
and the shared [safety/provenance contract](../../references/safety-and-provenance.md).
