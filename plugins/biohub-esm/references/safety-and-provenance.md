# Safety, licensing, and provenance contract

## Scientific claims

- Treat every structure, feature interpretation, mutation score, functional
  label, and designed binder as a model-generated hypothesis.
- Require experimental validation appropriate to the claim. Do not present a
  predicted structure as experimental truth, a static structure as molecular
  dynamics, an SAE label as established function, or an untrained classifier
  head as a biological prediction.
- ESMFold2 output is one static conformational hypothesis. It does not capture
  kinetics, ensembles, environment-dependent dynamics, binding affinity, or
  experimental uncertainty by itself.

## Biosafety and acceptable use

Follow the [Biohub Acceptable Use Policy](https://biohub.org/acceptable-use-policy/).
The managed platform may restrict controlled pathogen/toxin inputs. Do not
work around safeguards. Legitimate researchers affected by a restriction
should use Biohub's elevated-access process. Never infer that open weights
remove the user's responsibility to follow institutional, legal, and biosafety
review.

## Licensing

- Released ESM model code and weights are MIT licensed; confirm the exact model
  card and bundled notices at the pinned revision.
- ESM Atlas data is CC-BY-4.0. Preserve attribution, source hash/accession,
  retrieval timestamp, and dataset/API version in derived work.

## Required provenance sidecar

For every generated or downloaded artifact, record:

1. execution route (`atlas-api`, `atlas-s3`, `biohub`, `modal`, or `self-hosted`)
2. exact endpoint/base URL
3. exact managed model ID or Hugging Face repository and revision
4. pinned `Biohub/esm` and `Biohub/transformers` Git revisions when those
   dependencies execute locally or on Modal
5. SHA-256 digest of normalized input (do not duplicate sensitive input unless
   the user explicitly wants it stored)
6. complete inference parameters, MSA provenance, and seed
7. UTC start and finish timestamps
8. every output path, size, media type, and SHA-256 checksum
9. available confidence metrics: pLDDT, pAE, pTM, iPTM, pair-chain iPTM,
   similarity, or feature activation statistics
10. provider job/call ID when asynchronous

The helper CLI writes atomic JSON sidecars. Never put credentials,
`Authorization` headers, Modal config contents, or raw provider errors that
could echo a secret into provenance.

Use `null` for a local code revision when that dependency did not execute.
Before recording a revision for an installed SDK or model library, verify its
PEP 610 direct-VCS metadata or otherwise prove the installed commit; do not
infer it from documentation alone.
