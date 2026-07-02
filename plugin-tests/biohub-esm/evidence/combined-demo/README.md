# Combined live-demonstration evidence

The workflow folded a deterministic novel sequence with managed
`esmfold2-fast-2026-05`, derived Atlas SAE feature annotations, searched
related proteins, traversed a representative cluster, aligned two returned
structures, and rendered the results in the NGL Structure Viewer. The browser
capture was taken only after `document.body.dataset.ready == 'true'`; an
`error` state is a hard failure.

This is integration evidence, not an experimental or functional claim. The
fold is a static model hypothesis; Atlas similarity and learned-feature labels
require independent validation.

- normalized input SHA-256: `e25fcb5ad92dc674ee256f96ef43ba54c26eb759a526130f39ef9ff897929e64`
- mean pLDDT: `0.8095541596412659`
- pTM: `0.7633184194564819`
- archive: `artifacts.tar.gz`
- archive size: `574678` bytes
- archive SHA-256: `683228f2bf0b1e84bfb0f6965eb720100839c2c8c8680eb5272f734ef0a7b263`
- screenshot SHA-256: `9dd199983974024ffcaabdc1d6bfe8126c8eba5272c5566c6fcfa0c06a3c732a`
- allowlisted archive members: 17

Provenance paths retain their absolute live-run locations. After extraction,
relocate by archive-member basename and verify the recorded SHA-256; checksums
are the portable identity.

Atlas-derived JSON, structures, accessions, and annotations came from the
[public ESM Atlas API](https://biohub.ai/esm/protein/atlas/api-docs/overview.html)
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Aligned
structures and the workflow summary are adaptations; retain attribution and
identify changes. Released ESM code/weights are MIT licensed at their pinned
revisions.
