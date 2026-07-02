# Reliability and failure handling

## Normalized failure classes

| Failure | Response |
| --- | --- |
| missing credentials | stop only the credentialed route; continue public/mocked work |
| `401` | report authentication failure without echoing the response or header |
| `402` / credit denial | direct to account-specific developer console; do not invent quotas |
| `403` / safety restriction | do not work around; direct legitimate research to documented review |
| `408` / timeout | preserve job state and partial files, then bounded retry |
| `429` | honor `Retry-After`, lower concurrency, and resume later |
| `5xx` | retain durable IDs and raw redacted diagnostics; bounded retry |
| malformed JSON / missing fields | classify as schema drift; keep raw response |
| partial batch output | checksum completed artifacts, identify missing items, resume only those |
| expired async job | retain the terminal state and resubmit only after checking idempotency/cost |

## Durable execution

- Atlas batch jobs: persist `job_id`, status counts, and timestamps. Keep signed
  download URLs in memory only and reacquire them by polling after a resume.
- Modal jobs: persist each `FunctionCall` ID immediately after spawn. Gather can
  produce completed and failed results together. Cancellation is best effort;
  preserve late results if the provider already completed them.
- Downloads: write `.partial`, use HTTPS `Range` resume when supported, and
  atomically rename only after completion. Always compute SHA-256.

Never use an agent's manual reasoning loop as a background-job poller. Launch a
bounded CLI poller or use durable provider job state, then check on an explicit
cadence.
