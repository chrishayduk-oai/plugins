# ESMC self-hosting

Public IDs and pinned revisions are in
[`source-pins.md`](../../../references/source-pins.md). Public weights do not
require `HF_TOKEN`. Install both `Biohub/esm` and the Biohub Transformers fork
at the exact listed Git revisions; do not inherit the mutable upstream branch.

```python
import torch
from transformers import AutoModelForMaskedLM, AutoTokenizer

repo = "biohub/ESMC-300M"
revision = "a59b831785f907e96e6a246b1d142bfb76df31ee"
tokenizer = AutoTokenizer.from_pretrained(repo, revision=revision)
model = AutoModelForMaskedLM.from_pretrained(repo, revision=revision).eval()
inputs = tokenizer([sequence], return_tensors="pt", padding=True)
with torch.inference_mode():
    output = model(**inputs, output_hidden_states=True)
```

Use 300M for a low-cost smoke, then choose 600M or 6B only when the scientific
benefit and memory/latency cost are justified. For private/offline work, stage
the exact model snapshot and dependencies inside the controlled environment,
verify checksums, disable network access as required, and record the snapshot
revision. Do not send telemetry or input sequences outside the approved
boundary.

For bulk independent sequences on Modal, cache public weights on a Volume,
pin code/model revisions, preserve per-input seeds and digests, return exceptions
as partial results, and stay within current [Modal scaling limits](https://modal.com/docs/guide/scale).
