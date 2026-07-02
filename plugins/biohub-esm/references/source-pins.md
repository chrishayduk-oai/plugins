# Source pins and primary references

Verified 2026-07-01. These pins make examples reproducible; re-resolve and test
before deliberately upgrading them.

## Code and model revisions

| Artifact | Revision |
| --- | --- |
| `Biohub/esm` | `ba4d7124864eed323a93bf3cfefcd958f573b75a` |
| `Biohub/transformers` | `ef32577f55da19a4989cd7b22e004dc43a4998cb` |
| `biohub/ESMC-300M` | `a59b831785f907e96e6a246b1d142bfb76df31ee` |
| `biohub/ESMC-600M` | `a7e82012c83126b9eedb055fea9fa84b6c02f094` |
| `biohub/ESMC-6B` | `45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a` |
| `biohub/ESMFold2` | `1ebf0e3481a5184eb6171d40615c79e384b48796` |
| `biohub/ESMFold2-Fast` | `b28d8ace5e05e61e5bec1e6820cfd3e221819d12` |

The bounded binder smoke separately verifies Modal examples commit
`84939b0e7441198d16d3b37c937d18c0637b9729` and its two reviewed source-file
checksums before copying it to a temporary directory. The smoke pins:

| Binder artifact | Revision |
| --- | --- |
| `Biohub/esm` used by the official binder example | `f652b471d29da828b31e9b7a9cf7d0a7803240f5` |
| `biohub/ESMFold2-Experimental-Fast` | `04dec820ede9283c9893e318e5ca5a9ac2ab93bc` |
| `biohub/ESMFold2-Experimental-Fast-Cutoff2025` | `74b88548bf19688b8727432db0d698cb2e1d8783` |
| `biohub/ESMFold2-Experimental` | `0515a1177d6e6aab93750cbadf7b54da77bac592` |
| `biohub/ESMFold2-Experimental-Cutoff2025` | `56f94f5c1069ecde17512c96928850518340d287` |

The ESMC-6B and Transformers revisions remain the pins listed above. The
temporary copy injects every checkpoint revision because the reviewed upstream
example otherwise resolves model weights from mutable Hub defaults. It uses the
direct `main` entry point with scaling critics disabled.

Use the full revisions, not `main`, in installs and `from_pretrained(...,
revision=...)`. The pinned `Biohub/esm` package currently declares the Biohub
Transformers fork from mutable `main`. Install ESM first, then force-reinstall
exact Transformers with `--no-deps`; a combined resolver invocation can retain
`requested_revision = main` even when its resolved commit happens to match.
Verify both PEP 610 fields before execution. Record both code revisions and the
Hugging Face model revision in every self-hosted or Modal provenance sidecar.
Atlas and raw-HTTP paths record these code revisions as `null` because those
dependencies did not execute. Live SDK/local smokes verify PEP 610 install
metadata before asserting a local code commit.

## Primary sources

- [Biohub protein world model](https://biohub.ai/esm/protein)
- [Biohub ESM get-started guide](https://biohub.ai/esm/protein/get-started)
- [ESMC model page](https://biohub.ai/models/esmc)
- [ESMFold2 model page](https://biohub.ai/models/esmfold2)
- [Biohub managed logits API](https://biohub.ai/api-reference/logits)
- [ESM Atlas API overview](https://biohub.ai/esm/protein/atlas/api-docs/overview.html)
- [ESM Atlas API reference](https://biohub.ai/esm/protein/atlas/api-docs/api_reference.html)
- [Biohub/esm source](https://github.com/Biohub/esm)
- [ESMC-6B model card](https://huggingface.co/biohub/ESMC-6B)
- [ESMFold2 model card](https://huggingface.co/biohub/ESMFold2)
- [Modal ESMFold2 example](https://modal.com/docs/examples/esmfold2)
- [Modal ESMFold2 binder-design example](https://modal.com/docs/examples/esmfold2_binder_design)
- [Modal authentication configuration](https://modal.com/docs/sdk/py/latest/modal.config)
- [Modal scale-out guide and limits](https://modal.com/docs/guide/scale)
- [Language Modeling Materializes a World Model of Protein Biology, bioRxiv v1](https://www.biorxiv.org/content/10.64898/2026.06.03.729735v1)

The Atlas reference explicitly labels the API alpha, unauthenticated, mutable,
and unsuitable for production assumptions. Preserve raw responses alongside
normalized artifacts so schema drift can be diagnosed.
