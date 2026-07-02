# ESMFold2 managed and SDK contract

Primary sources: [ESMFold2 model page](https://biohub.ai/models/esmfold2),
[ESMFold2 model card](https://huggingface.co/biohub/ESMFold2), and
[Biohub/esm](https://github.com/Biohub/esm).

## Models

- managed full: `esmfold2-2026-05`
- managed fast: `esmfold2-fast-2026-05`
- open full: `biohub/ESMFold2`
- open fast: `biohub/ESMFold2-Fast`

Use the pinned official `SequenceStructureForgeInferenceClient` or
`esmfold2_client` for `POST /api/v1/fold_all_atom`. Supply `ESM_API_KEY` from the
environment only.

```python
import os

from esm.sdk import esmfold2_client
from esm.sdk.api import FoldingConfig
from esm.utils.structure.input_builder import ProteinInput, StructurePredictionInput

client = esmfold2_client(
    model="esmfold2-fast-2026-05",
    token=os.environ["ESM_API_KEY"],
)
fold_input = StructurePredictionInput(
    sequences=[ProteinInput(id="A", sequence=sequence)]
)
result = client.fold_all_atom(
    fold_input,
    config=FoldingConfig(num_loops=20, num_sampling_steps=100),
)
```

## Hosted bounds verified 2026-07-01

| Field | Default | Range |
| --- | ---: | ---: |
| `num_loops` | 20 | 0–20 |
| `num_sampling_steps` | 100 | 1–100 |
| `lm_dropout` | 0.3 | 0–1 |
| `lm_mask_pct` | full 0, Fast 0.1 when omitted | 0–1 |
| `msa_max_depth` | 1024 | 1–16,384 or null |
| `msa_column_mask_rate` | 0.1 | 0–1 |

Boolean options include PAE, distogram, pair-chain iPTM, and embeddings where
the endpoint/model returns them. Exact returned fields can evolve; preserve the
raw response and handle absent optional metrics. Do not send an MSA to Fast:
the pinned SDK warns that it will be ignored, and this plugin treats that as a
validation error to prevent a false scientific assumption.

The web Fold tool currently limits entry to 700 residues. That is a UI boundary,
not a documented universal architectural limit for managed or open ESMFold2.
