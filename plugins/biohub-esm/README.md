# Biohub ESM plugin

This plugin routes protein-language-model, structure-prediction, public Atlas,
and binder-design requests to the scientifically appropriate ESM workflow:

- Biohub managed API for modest interactive ESMC and ESMFold2 inference
- ESM Atlas public alpha API or anonymous S3 for discovery and public data
- Modal for bulk, parallel, long-running folds and binder-design campaigns
- pinned Hugging Face weights on user-owned compute for private, offline,
  data-resident, customized, fine-tuned, or sustained workloads

It intentionally does not add a hosted MCP server. The small standard-library
client under `scripts/` handles deterministic routing, validation, secret-safe
preflight, Atlas HTTP, durable Modal job state, and provenance. Managed model
examples use Biohub's pinned official `esm` SDK.

## Local validation

These commands are for a repository source checkout. The top-level
`plugin-tests/biohub-esm/` harness and repository validator are intentionally
not included in an installed plugin bundle.

```bash
python3 -m unittest discover -s plugin-tests/biohub-esm -v
python3 plugins/biohub-esm/scripts/biohub_esm.py preflight
node plugins/plugin-eval/scripts/plugin-eval.js analyze plugins/biohub-esm --format markdown
```

Live smokes are opt-in. They never ask for or print credentials:

```bash
python3 plugin-tests/biohub-esm/live_smoke.py atlas --output-dir /tmp/biohub-esm-atlas-smoke
python3 plugin-tests/biohub-esm/live_smoke.py biohub-esmc --output-dir /tmp/biohub-esm-esmc-smoke
python3 plugin-tests/biohub-esm/live_smoke.py biohub-esmfold2 --output-dir /tmp/biohub-esm-fold-smoke
```

Modal fold and one-seed binder smokes are separately gated by native Modal
authentication, hardware billing, exact upstream pins, and explicit cost
confirmation. The binder smoke is integration evidence only—not a useful
campaign or an efficacy result.

In a source checkout, read `plugin-tests/biohub-esm/README.md` for credential
and hardware gates. Installed copies remain self-contained and do not rely on
that repository-only path.
