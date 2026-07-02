"""Deterministic intent classifier used only for activation regression tests."""

from __future__ import annotations

import re


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def select_skill(prompt: str) -> str | None:
    text = prompt.strip()
    if not text:
        return None
    if _has(text, r"\b(boltz|alphafold|clustal|blast|gpt|language atlas|road atlas)\b") and not _has(
        text, r"\b(biohub|esmc|esmfold2|esm atlas)\b"
    ):
        return None
    if _has(text, r"\b(auth|authenticate|api key|credentials?|install|setup|preflight|rate limit|credits?)\b") and _has(
        text, r"\b(biohub|esm|modal|hugging face|atlas)\b"
    ):
        return "biohub-esm-setup"
    if _has(text, r"\b(binder|binders|minibinder|minibinders|scfv|antibody design)\b") and _has(
        text, r"\b(design|campaign|generate|invert|screen|modal|esmfold2)\b"
    ):
        return "esmfold2-binder-design"
    if _has(text, r"\b(esm atlas|atlas api|atlas cluster|protein hash|md5|feature catalog|anonymous s3)\b") or (
        _has(text, r"\b(similar|related|discover|search)\b")
        and _has(text, r"\b(proteins?|sae|functional signature|clusters?)\b")
    ):
        return "esm-atlas"
    if _has(text, r"\b(esmfold2|fold|folding|all-atom|plddt|pae|ptm|iptm|biomolecular complex)\b"):
        return "esmfold2"
    if _has(
        text,
        r"\b(esmc|embedding|hidden states?|masked[- ]residue|logits|entropy|mutation scoring|sae features?)\b",
    ):
        return "esmc"
    if _has(text, r"\b(biohub esm|esm models?|which esm|private gpu|route.*compute|500 designed sequences)\b"):
        return "biohub-esm"
    return None
