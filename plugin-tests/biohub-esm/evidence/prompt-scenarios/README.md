# Clean Codex install and activation evidence

Generated `2026-07-02T02:46:27.850151Z` with `codex-cli 0.143.0-alpha.33`, model
`gpt-5.6-sol`, reasoning effort `xhigh`, and a read-only sandbox.
The marketplace and plugin were installed into a newly created isolated Codex
home. The harness neither inspected, copied, nor persisted the existing Codex
auth file; Codex consumed it in place through a symlink. `HOME` pointed to a new
empty directory, and scientific-provider credentials/profiles were not inherited.

The installed plugin matched source tree SHA-256
`1a5cd775b669843327a79e90278373a2138f25ff5580272535f2d14be39859bd` across 46 files (cache/bytecode
excluded). The checksummed `artifacts.tar.gz` contains each
exact prompt, raw Codex JSONL event trace, final answer,
command/config/exit record, timestamps, install evidence, and checksums. Its
SHA-256 is `c42e4cb39f76cddc5b5eee321d2e52e264aa4705fe5dbfb8165c8be2026182ad` (69621 bytes).
All 5 deterministic scenarios passed, including the
adjacent negative prompt.
