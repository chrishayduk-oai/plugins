---
name: esmc
description: Use when the user needs ESMC embeddings, hidden states, logits, entropy, zero-shot mutation scoring, SAE features, fitted heads, or fine-tuning. Sequence only; not for folding, Atlas lookup, or calling an untrained classifier predictive.
---

# ESMC

ESMC is a sequence-only masked protein language model. Validate the input before
inference:

```bash
python3 <plugin-root>/scripts/biohub_esm.py validate-sequence \
  --target esmc --sequence-file /absolute/path/query.fasta
```

The context is 2,048 tokens. Because current tokenizers add BOS/EOS, the helper
uses a conservative 2,046-residue raw-sequence cap unless a pinned tokenizer
probe proves a different count. Do not pass structures, DNA, RNA, or ligands.

## Model and route

| Route | IDs |
| --- | --- |
| Biohub managed | `esmc-300m-2024-12`, `esmc-600m-2024-12`, `esmc-6b-2024-12` |
| Hugging Face | `biohub/ESMC-300M`, `biohub/ESMC-600M`, `biohub/ESMC-6B` |

Default to managed ESMC for modest interactive calls. Use Modal for independent
bulk/parallel work and pinned Hugging Face weights for private, offline,
custom-head, fine-tuned, or sustained workloads.

## Analysis contract

1. Preserve the normalized sequence digest and exact model/revision.
2. Request only the outputs needed: sequence logits, per-residue embedding,
   mean embedding, selected hidden states, or named SAE models.
3. For entropy, mask the evaluated residue and compute categorical entropy from
   the returned amino-acid distribution.
4. For zero-shot mutation scoring, report the documented score definition,
   typically `log P(mutant | masked context) - log P(wild type | masked context)`.
   Keep raw log probabilities and residue numbering.
5. Treat embeddings and SAE activations as features, not biological labels.
6. A downstream classifier/regressor becomes a prediction only after fitting on
   appropriate labeled data and validating on held-out, leakage-controlled data.
   Never run an untrained classification head and name its random output.
7. Record outputs and provenance atomically; large tensors should be files with
   checksums, not pasted into chat.

Read the exact [managed/SDK contract](references/api.md),
[analysis guidance](references/analysis.md), and
[self-hosting guidance](references/self-hosted.md).
