"""Pinned model identifiers and public service constants.

Pins were resolved from the public upstreams on 2026-07-01. Callers should
record them in provenance and intentionally update them when validating a new
upstream revision; never silently replace them with ``main``.
"""

from __future__ import annotations

BIOHUB_BASE_URL = "https://biohub.ai"
ATLAS_API_PREFIX = "/esm/protein/api/v1alpha1"
ATLAS_API_VERSION = "v1alpha1"
ATLAS_SCHEMA_VERSION = "0.1.0"

ESM_GIT_REVISION = "ba4d7124864eed323a93bf3cfefcd958f573b75a"
TRANSFORMERS_GIT_REVISION = "ef32577f55da19a4989cd7b22e004dc43a4998cb"

ESMC_MANAGED_MODELS = (
    "esmc-300m-2024-12",
    "esmc-600m-2024-12",
    "esmc-6b-2024-12",
)
ESMC_HF_MODELS = (
    "biohub/ESMC-300M",
    "biohub/ESMC-600M",
    "biohub/ESMC-6B",
)
ESMFOLD2_MANAGED_MODELS = (
    "esmfold2-2026-05",
    "esmfold2-fast-2026-05",
)
ESMFOLD2_HF_MODELS = (
    "biohub/ESMFold2",
    "biohub/ESMFold2-Fast",
)

HF_REVISIONS = {
    "biohub/ESMC-300M": "a59b831785f907e96e6a246b1d142bfb76df31ee",
    "biohub/ESMC-600M": "a7e82012c83126b9eedb055fea9fa84b6c02f094",
    "biohub/ESMC-6B": "45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a",
    "biohub/ESMFold2": "1ebf0e3481a5184eb6171d40615c79e384b48796",
    "biohub/ESMFold2-Fast": "b28d8ace5e05e61e5bec1e6820cfd3e221819d12",
}

# The Modal binder example uses experimental inversion and critic checkpoints,
# not the standard ESMFold2 checkpoint. Its upstream source and every public
# weight are pinned independently from the general inference stack.
MODAL_BINDER_EXAMPLE_REVISION = "84939b0e7441198d16d3b37c937d18c0637b9729"
MODAL_BINDER_ESM_GIT_REVISION = "f652b471d29da828b31e9b7a9cf7d0a7803240f5"
MODAL_BINDER_HF_REVISIONS = {
    "biohub/ESMC-6B": HF_REVISIONS["biohub/ESMC-6B"],
    "biohub/ESMFold2-Experimental-Fast": "04dec820ede9283c9893e318e5ca5a9ac2ab93bc",
    "biohub/ESMFold2-Experimental-Fast-Cutoff2025": "74b88548bf19688b8727432db0d698cb2e1d8783",
    "biohub/ESMFold2-Experimental": "0515a1177d6e6aab93750cbadf7b54da77bac592",
    "biohub/ESMFold2-Experimental-Cutoff2025": "56f94f5c1069ecde17512c96928850518340d287",
}
MODAL_BINDER_SOURCE_SHA256 = {
    "06_gpu_and_ml/binder-design/esmfold2_binder_design.py": (
        "34168dfac61c81748b9f37d54cc5d03b6e7285a4d5b52efadc023fccc5e3b9ac"
    ),
    "06_gpu_and_ml/binder-design/binder_design/models.py": (
        "71c3abbce7e044fe1f6a2819ef93841d0babba996246c12f9ff7ab6572e6d3e8"
    ),
}

ESMC_MAX_TOKENS = 2048
# Hugging Face tokenizers add BOS and EOS. Without a tokenizer probe, keeping
# two positions free is the safe raw-residue limit.
ESMC_CONSERVATIVE_MAX_RESIDUES = 2046
ATLAS_SEARCH_MAX_RESIDUES = 800
ATLAS_FOLD_MAX_RESIDUES = 699
ATLAS_FEATURE_COUNT = 16_384
ATLAS_BATCH_MAX_UNIQUE_HASHES = 500

HOSTED_FOLD_BOUNDS = {
    "num_loops": (0, 20),
    "num_sampling_steps": (1, 100),
    "lm_dropout": (0.0, 1.0),
    "lm_mask_pct": (0.0, 1.0),
    "msa_max_depth": (1, 16_384),
    "msa_column_mask_rate": (0.0, 1.0),
}

DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_TIMEOUT_SECONDS = 1800.0

PROTEIN_ALPHABET = frozenset("ACDEFGHIKLMNPQRSTVWYX")
DNA_ALPHABET = frozenset("ACGTN")
RNA_ALPHABET = frozenset("ACGUN")

MANAGED_ENDPOINTS = frozenset({"encode", "logits", "fold", "fold_all_atom"})
