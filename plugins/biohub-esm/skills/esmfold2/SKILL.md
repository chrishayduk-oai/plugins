---
name: esmfold2
description: Use when the user needs ESMFold2 all-atom folding for proteins, DNA, RNA, modified residues, or ligands, including Fast/full routing, MSA, confidence, structures, and provenance. Not for dynamics, experimental truth, Atlas discovery, or binder campaigns.
---

# ESMFold2

## Choose the model

| Need | Managed | Hugging Face |
| --- | --- | --- |
| accuracy, difficult complexes, or optional MSA | `esmfold2-2026-05` | `biohub/ESMFold2` |
| fast single-sequence throughput | `esmfold2-fast-2026-05` | `biohub/ESMFold2-Fast` |

Fast is not MSA-conditioned. If the user supplies or requires an MSA, route to
full ESMFold2 and validate query alignment. Missing MSA is not an error for a
single-sequence fold unless the user explicitly required MSA conditioning.

## Build and validate input

Use the official pinned SDK's `StructurePredictionInput` with `ProteinInput`,
`DNAInput`, `RNAInput`, `LigandInput`, and zero-based `Modification` positions.
Each chain needs a unique ID. A ligand uses either SMILES or CCD identifiers.

```bash
python3 <plugin-root>/scripts/biohub_esm.py validate-fold \
  --model esmfold2-2026-05 \
  --input /absolute/path/fold-input.json \
  --config /absolute/path/folding-config.json
```

Do not universalize the Biohub web UI's 700-residue entry cap as an architectural
model limit. Validate current hosted parameters exactly: loops 0–20, sampling
steps 1–100, LM dropout/mask fraction 0–1, MSA depth 1–16,384 or null, and MSA
column mask fraction 0–1.

## Outputs

Prefer mmCIF for all-atom complexes; PDB can be lossy for complex chemistry.
Preserve coordinates, pLDDT, pAE, pTM, iPTM, pair-chain iPTM, and requested
distograms/embeddings when returned. State the scale emitted by the pinned SDK
instead of assuming pLDDT is 0–100; current ESMFold2 examples emit 0–1 values.

Explain that the result is a static model hypothesis, not dynamics, affinity,
or experimental truth. Low confidence, disorder, interfaces, ligands, modified
residues, and unexpected topology require special caution and experimental
validation.

Read the [managed/SDK contract](references/api.md),
[inputs and results](references/inputs-and-results.md), and
[self-hosting/Modal guidance](references/self-hosted.md).
