---
name: biohub-esm-setup
description: Set up and preflight Biohub ESM, Atlas, Modal, and Hugging Face access. Use when installing pinned ESM dependencies, checking credentials, or fixing auth, rate-limit, credits, PATH, SDK, or GPU setup. Never ask for secret values.
---

# Biohub ESM setup and auth

## Safe preflight

Run:

```bash
python3 <plugin-root>/scripts/biohub_esm.py preflight
```

It reports only `configured`, `missing`, `optional-missing`, or `not-required`.
Never open or display `.modal.toml`; file presence is enough for preflight.

## Credentials

- Biohub managed ESMC/ESMFold2: `ESM_API_KEY` from the developer console.
  Configure it with the host secret manager. Never ask the user to paste it.
- Atlas API and `s3://esm-protein-atlas/v1/`: no client credential in the
  current alpha.
- Modal: `MODAL_TOKEN_ID` plus `MODAL_TOKEN_SECRET`, or an authenticated Modal
  profile. Environment values override the profile.
- Hugging Face public weights: no token required. `HF_TOKEN` is optional for
  authenticated Hub access and rate-limit handling.

## Reproducible install

Use an isolated environment and the pin in
[`source-pins.md`](../../references/source-pins.md):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install \
  "esm @ git+https://github.com/Biohub/esm.git@ba4d7124864eed323a93bf3cfefcd958f573b75a"
.venv/bin/python -m pip install --force-reinstall --no-deps \
  "transformers @ git+https://github.com/Biohub/transformers.git@ef32577f55da19a4989cd7b22e004dc43a4998cb"
.venv/bin/python <plugin-root>/scripts/biohub_esm.py verify-install
```

ESM currently declares the Transformers fork from mutable `main`. The second
layer must replace it with the exact direct-VCS revision; a single combined
install is not reproducible even if `commit_id` happens to match today.

For Modal, use an already authenticated official CLI or install the official
SDK in an isolated environment. Do not place tokens on a command line. The
[Modal config reference](https://modal.com/docs/sdk/py/latest/modal.config)
documents environment and profile resolution.

## Diagnose without weakening tests

- `401`: missing, invalid, or expired Biohub key. Report auth failure only.
- `402` or credit message: account-specific credits; direct the user to the
  developer console without inventing a quota.
- `429`: honor `Retry-After`; reduce concurrency or resume later.
- timeout/5xx: keep durable state and partial artifacts, then retry with bounded
  backoff.
- Atlas schema error: retain raw response, compare the live alpha schema, and
  update validation intentionally.
- OOM/hardware mismatch: select a smaller ESMC model, ESMFold2-Fast, Modal, or
  suitable user-owned GPU; do not silently lower scientific parameters.

Read [setup details](references/setup.md), the shared
[source pins](../../references/source-pins.md), and
[safety/provenance contract](../../references/safety-and-provenance.md).
