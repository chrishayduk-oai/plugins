# Setup details

Primary sources: [Biohub get started](https://biohub.ai/esm/protein/get-started),
[Biohub/esm](https://github.com/Biohub/esm),
[Modal config](https://modal.com/docs/sdk/py/latest/modal.config), and
[Hugging Face ESMC-6B](https://huggingface.co/biohub/ESMC-6B).

## Runtime matrix

| Route | Required | Optional / note |
| --- | --- | --- |
| Atlas API | HTTPS client | no client key in current alpha |
| Atlas S3 | AWS CLI | use `--no-sign-request` |
| Biohub | pinned `esm`, `ESM_API_KEY` | credits/rate limits are account-specific |
| Modal | official Modal SDK/CLI and profile or token env pair | public weights do not need `ESM_API_KEY` |
| Self-hosted HF | pinned `esm`, PyTorch/Transformers, suitable compute | `HF_TOKEN` optional for public repos |

## Do not expose secrets

- Never accept a secret as a CLI flag, generated file, test fixture, screenshot,
  chat message, PR body, or Linear comment.
- Do not run `env`, `set`, config dumps, or commands that interpolate a secret.
- Do not inspect `.modal.toml`; a native `modal profile current`/authenticated
  API check or simple file-presence preflight is sufficient.
- Redact nested keys containing authorization, API key, token, secret, or
  password, and redact Bearer strings before diagnostics are persisted.

## Pin verification

```bash
python3 <plugin-root>/scripts/biohub_esm.py pins
python3 <plugin-root>/scripts/biohub_esm.py verify-install
```

Compare against [`source-pins.md`](../../../references/source-pins.md) and update
code, references, mocks, live smokes, and provenance together. Do not update a
pin from a mutable branch without rerunning the entire validation loop.
`verify-install` requires both the resolved `commit_id` and PEP 610
`requested_revision` to equal the full pin; matching only the current tip of a
mutable branch is insufficient.
