---
name: esmfold2-binder-design
description: Use when the user needs ESMFold2 inversion for minibinder, binder, or scFv campaigns, Modal scale-out, candidate selection, persistence, or private self-hosting. Never route to Biohub managed API; not for ordinary folding or single-seed efficacy claims.
---

# ESMFold2 binder design

Binder design is not available through the Biohub managed API today. Route only
to the released open-weight workflow on Modal or suitable user-owned compute.
Modal public-weight execution needs Modal authentication, not `ESM_API_KEY`.

## Campaign contract

1. Validate target sequence/structure, binder modality (minibinder or scFv),
   templates, constraints, and biosafety/AUP fit.
2. Pin `Biohub/esm`, ESMFold2/ESMC weights, helper code, and every seed.
3. Explain scale honestly: useful campaigns normally need hundreds and often
   about 1,000 designs across seeds/templates. A one-seed smoke test verifies
   integration only and is not representative screening depth or efficacy.
4. Before material GPU spend, show candidate count, GPUs, timeout, persistence,
   and provider cost evidence; obtain explicit confirmation.
5. Spawn independent jobs, persist every call ID immediately, gather with
   partial-failure handling, and support cancellation/resume.
6. Preserve sequences, trajectories, structures, raw metrics, selection table,
   exact critic configuration, and provenance. Do not select on one metric
   alone or claim affinity from structural confidence.
7. Require experimental screening and appropriate safety review before any
   biological conclusion.

The official Modal example uses an H100, persistent model/results Volumes,
`spawn()` plus `FunctionCall.gather`, deterministic algorithms, and a server-side
selection pass. The model plus four critic models are roughly 50 GB. A bounded
live smoke may use exactly one seed and batch size one only after cost is visible.

Read [Modal execution](references/modal.md),
[campaign design and interpretation](references/campaign.md), and shared
[safety/provenance](../../references/safety-and-provenance.md).
