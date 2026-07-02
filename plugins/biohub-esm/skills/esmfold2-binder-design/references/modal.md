# Modal binder-design execution

Primary sources: [official binder-design example](https://modal.com/docs/examples/esmfold2_binder_design),
[Modal authentication](https://modal.com/docs/sdk/py/latest/modal.config), and
[Modal scaling limits](https://modal.com/docs/guide/scale).

The official example:

- builds a pinned Biohub ESM image with deterministic CUDA settings
- caches roughly 50 GB of ESMC/ESMFold2/critic weights on a Volume
- stores result tables on a separate persistent Volume
- runs one `design` call per seed/template/target
- fans out with `spawn()` and gathers with `FunctionCall.gather`
- ranks candidates with combined interface/distogram evidence

Update upstream pins to the revisions validated by this plugin. Do not execute a
mutable `main` or silently reuse stale cached weights.

## Durable control plane

The helper's `modal-jobs` command targets a deployed function that accepts
keyword payloads. It is control-plane-only: it persists call IDs before gather
but does not infer scientific provenance from arbitrary return values:

```bash
python3 <plugin-root>/scripts/biohub_esm.py modal-jobs spawn \
  --app-name <deployed-app> --function-name <function> \
  --kind binder-design --input /absolute/path/payloads.json \
  --state /absolute/path/modal-jobs.json --max-jobs <confirmed-count> \
  --confirm-cost
```

Use the same state file for `gather` or `cancel`; those commands resolve the
persisted `FunctionCall` IDs directly and do not require app/function names.
Cancellation remains `cancellation-requested` until a later gather reconciles a
late result or provider-confirmed cancellation. Never pass Modal tokens on the
command line. The native profile or token environment pair is sufficient.

There is one unavoidable RPC ambiguity: a client can die after Modal accepts a
spawn but before the call ID is persisted. On reload, a durable `spawning`
entry becomes `submission-indeterminate`, records app/function and payload
digest for manual dashboard reconciliation, and must never be auto-resubmitted.
This prevents a retry from silently duplicating GPU spend when acceptance is
unknown. A timeout, disconnect, or other unclassified exception returned by
`spawn()` is treated the same way because it can also occur after provider
acceptance; it is not mislabeled as a definitive failure.

For a call to become `completed`, the deployed function must return exactly a
compact `{ "submission_sha256": ..., "result": ..., "provenance": ... }`
envelope. Both the envelope and provenance input digest must match the submitted
payload; the provenance endpoint must match the deployed app/function. It must
also pass the shared schema, include the `modal` route, applicable pinned
model/code/HF revisions and timestamps, and at least one artifact with path,
size, media type, and SHA-256. Binder envelopes must record the complete critic
model revision map. Large data should remain in durable storage and be
referenced by that checksummed metadata. Bare JSON, stale/wrong-input results,
incomplete provenance, and non-JSON results fail closed; the durable call stays
auditable rather than becoming false scientific evidence.

Modal documents up to 1,000 concurrent inputs per map invocation and larger
limits for spawned async calls, but these are service limits, not a spending
recommendation. Use bounded concurrency and inspect current account/provider
limits. A single-seed smoke is allowed only as an integration check after cost
is visible; never launch a full campaign without explicit confirmation.

## Bounded smoke cost

The [Modal pricing page](https://modal.com/pricing) listed H100 compute at
`$0.001097/second` (`$3.9492/hour`) when verified on 2026-07-01. The direct
one-design smoke uses one H100, batch size one, seed zero, and the official
one-hour timeout, so its hard GPU-only upper bound is `$3.95`; CPU, memory,
Volume storage, and download charges are additional. Modal bills actual
seconds, but pricing can change, so refresh the page and obtain confirmation
before running.

The opt-in harness verifies the official Modal examples checkout and source
hashes, creates a temporary copy, then pins Transformers, ESMC-6B, and every
experimental inversion/critic checkpoint. It rejects an unreviewed checkout.
Run exactly one seed only after confirming the refreshed bound:

```bash
python3 <repo-root>/plugin-tests/biohub-esm/live_smoke.py modal-binder \
  --example-root /absolute/path/to/modal-examples \
  --output-dir /absolute/path/to/evidence \
  --max-gpu-cost-usd 3.9492 --confirm-cost --confirm-volume-cleanup
```

`--max-gpu-cost-usd` records the confirmed GPU-only ceiling; it is not a
provider-enforced budget. The one-hour Modal function timeout provides the hard
GPU-time bound. Abort if current pricing makes that ceiling stale.

The smoke rewrites the example to ticket-scoped model/results Volumes
`lsc110-biohub-esm-binder-models-v1` and
`lsc110-biohub-esm-binder-results-v1`, plans for at most 55 GiB combined, and
deletes both after every outcome. At the 2026-07-01 list price of
`$0.09/GiB/month` (with 1 TiB/month included), retaining that ceiling would
list at `$4.95/month`; the harness does not intentionally retain it. Per
[Modal's Volume pricing documentation](https://modal.com/docs/guide/volumes#pricing),
usage is snapshotted daily, so deleted storage may still be billed for up to
four days. Cleanup bounds retention but does not imply zero post-delete cost.

Use the official `main` entry point for this smoke. Do not use the `sweep`
entry point: even with one seed, its GPU orchestrator can overlap a second H100
design worker and is unnecessarily expensive. Modal also requires a payment
method on file before allocating GPU functions.
