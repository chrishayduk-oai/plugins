# Test matrix

Run deterministic tests without credentials:

```bash
python3 -m unittest discover -s plugin-tests/biohub-esm -p 'test_*.py' -v
```

## Opt-in live routes

The harness never accepts credentials as arguments or writes them to artifacts.

| Command | Gate |
| --- | --- |
| `live_smoke.py atlas` | public network only |
| `live_smoke.py biohub-esmc` | `ESM_API_KEY` in login-shell environment; pinned `esm` SDK |
| `live_smoke.py biohub-esmfold2` | same |
| `live_smoke.py modal-fold` | Modal/payment method, price review, cost ceiling, and ticket-volume cleanup confirmation |
| `live_smoke.py modal-binder` | Same gates; reviewed direct one-H100, one-seed call |
| `live_smoke.py hf-esmc` | network, disk/RAM, pinned transformers stack; no HF token required |
| `live_smoke.py hf-esmfold2` | CUDA hardware; no HF token required |

The binder smoke is deliberately non-representative. It does not justify a hit
rate or efficacy claim. Refresh current Modal pricing before confirming it; on
2026-07-01 the H100 GPU-only bounds were $1.3164 for the 20-minute fold and
$3.9492 for the one-hour binder smoke. The combined public demonstration script produces
separate raw artifacts, normalized result JSON, aligned PDB files, a viewer, and
provenance; it never substitutes mock output for a live result.
