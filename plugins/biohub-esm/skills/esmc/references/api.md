# ESMC managed and SDK contract

Primary sources: [ESMC model page](https://biohub.ai/models/esmc),
[managed logits reference](https://biohub.ai/api-reference/logits),
[Biohub/esm](https://github.com/Biohub/esm), and
[ESMC-6B model card](https://huggingface.co/biohub/ESMC-6B).

## Managed models

- `esmc-300m-2024-12`
- `esmc-600m-2024-12`
- `esmc-6b-2024-12`

`POST /api/v1/logits` uses Bearer authentication and a JSON body containing the
managed model, encoded `inputs`, and `logits_config`. Use the pinned official SDK
to encode raw sequences rather than recreating token IDs.

```python
import os

from esm.sdk import esmc_client
from esm.sdk.api import ESMProtein, LogitsConfig

client = esmc_client(
    model="esmc-600m-2024-12",
    url="https://biohub.ai",
    token=os.environ["ESM_API_KEY"],
)
protein = ESMProtein(sequence=sequence)
tokens = client.encode(protein)
result = client.logits(
    tokens,
    LogitsConfig(
        sequence=True,
        return_embeddings=True,
        return_hidden_states=False,
        return_mean_embedding=False,
    ),
)
```

Never put the key in source or a command. `LogitsConfig` supports sequence
logits, per-residue/final embeddings, mean embeddings, selected/all hidden
states subject to model restrictions, and `SAEConfig`. Structure, secondary
structure, SASA, and function logits are not supported on the managed endpoint.

The current pinned SDK notes that ESMC-6B managed inference cannot return all
hidden layers at once; request one valid `ith_hidden_layer`. Layer 0 is the
embedding layer. Check the pinned SDK for the current layer maximum before a
layer sweep.

Errors can return a model error object instead of the expected tensor result.
Check that path before accessing outputs and retain only redacted diagnostics.
