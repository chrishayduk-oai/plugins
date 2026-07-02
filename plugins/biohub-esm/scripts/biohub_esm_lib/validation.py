"""Conservative validation for ESMC, ESMFold2, and Atlas inputs."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .constants import (
    ATLAS_BATCH_MAX_UNIQUE_HASHES,
    ATLAS_FEATURE_COUNT,
    ATLAS_FOLD_MAX_RESIDUES,
    ATLAS_SEARCH_MAX_RESIDUES,
    DNA_ALPHABET,
    ESMC_CONSERVATIVE_MAX_RESIDUES,
    ESMC_MANAGED_MODELS,
    ESMFOLD2_HF_MODELS,
    ESMFOLD2_MANAGED_MODELS,
    HOSTED_FOLD_BOUNDS,
    PROTEIN_ALPHABET,
    RNA_ALPHABET,
)
from .errors import ValidationError

MD5_RE = re.compile(r"^[0-9a-f]{32}$")


def _reject_unknown_fields(
    value: Mapping[str, Any], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"unsupported {context} fields: {', '.join(unknown)}")


def normalize_sequence(value: str) -> str:
    """Normalize one raw or single-record FASTA sequence without guessing gaps."""

    if not isinstance(value, str):
        raise ValidationError("sequence must be a string")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if lines and lines[0].startswith(">"):
        if any(line.startswith(">") for line in lines[1:]):
            raise ValidationError("expected one FASTA record, but multiple headers were found")
        lines = lines[1:]
    if any(line.startswith(">") for line in lines):
        raise ValidationError("expected one FASTA record, but multiple headers were found")
    sequence = "".join(lines).replace(" ", "").upper()
    if not sequence:
        raise ValidationError("sequence is empty")
    return sequence


def _validate_alphabet(sequence: str, alphabet: frozenset[str], modality: str) -> None:
    invalid = sorted(set(sequence) - alphabet)
    if invalid:
        rendered = "".join(invalid[:12])
        raise ValidationError(f"{modality} sequence contains unsupported symbols: {rendered}")


def validate_protein_sequence(value: str, *, max_residues: int | None = None) -> str:
    sequence = normalize_sequence(value)
    _validate_alphabet(sequence, PROTEIN_ALPHABET, "protein")
    if max_residues is not None and len(sequence) > max_residues:
        raise ValidationError(
            f"protein sequence has {len(sequence)} residues; conservative limit is {max_residues}"
        )
    return sequence


def validate_esmc_sequence(value: str) -> str:
    return validate_protein_sequence(value, max_residues=ESMC_CONSERVATIVE_MAX_RESIDUES)


def validate_atlas_search_sequence(value: str) -> str:
    return validate_protein_sequence(value, max_residues=ATLAS_SEARCH_MAX_RESIDUES)


def validate_atlas_fold_sequence(value: str) -> str:
    return validate_protein_sequence(value, max_residues=ATLAS_FOLD_MAX_RESIDUES)


def sequence_md5(value: str) -> str:
    sequence = validate_protein_sequence(value)
    return hashlib.md5(sequence.encode("ascii"), usedforsecurity=False).hexdigest()


def validate_md5(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("protein hash must be a lowercase 32-character MD5 digest")
    normalized = value.strip().lower()
    if not MD5_RE.fullmatch(normalized):
        raise ValidationError("protein hash must be a lowercase 32-character MD5 digest")
    return normalized


def validate_feature_index(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("feature index must be an integer")
    if not 0 <= value < ATLAS_FEATURE_COUNT:
        raise ValidationError(f"feature index must be between 0 and {ATLAS_FEATURE_COUNT - 1}")
    return value


def validate_batch_hashes(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValidationError("protein_hashes must be a sequence of MD5 strings")
    normalized = [validate_md5(value) for value in values]
    unique = list(dict.fromkeys(normalized))
    if not unique:
        raise ValidationError("protein_hashes must contain at least one hash")
    if len(unique) > ATLAS_BATCH_MAX_UNIQUE_HASHES:
        raise ValidationError(
            f"Atlas accepts at most {ATLAS_BATCH_MAX_UNIQUE_HASHES} unique hashes per batch"
        )
    return unique


def _number_in_range(name: str, value: Any, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be numeric")
    if not low <= value <= high:
        raise ValidationError(f"{name} must be within [{low}, {high}]")


def validate_hosted_fold_config(config: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "include_distogram",
        "include_pae",
        "include_pair_chains_iptm",
        "include_embeddings",
        *HOSTED_FOLD_BOUNDS.keys(),
    }
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValidationError(f"unsupported hosted folding parameters: {', '.join(unknown)}")
    result = dict(config)
    for name in (
        "include_distogram",
        "include_pae",
        "include_pair_chains_iptm",
        "include_embeddings",
    ):
        if name in result and not isinstance(result[name], bool):
            raise ValidationError(f"{name} must be boolean")
    for name, bounds in HOSTED_FOLD_BOUNDS.items():
        if name not in result or (name == "msa_max_depth" and result[name] is None):
            continue
        _number_in_range(name, result[name], *bounds)
        if name in {"num_loops", "num_sampling_steps", "msa_max_depth"} and not isinstance(
            result[name], int
        ):
            raise ValidationError(f"{name} must be an integer")
    return result


def _validate_chain_ids(raw: Any) -> list[str]:
    ids = [raw] if isinstance(raw, str) else raw
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
        raise ValidationError("each entity id must be a non-empty string or list of strings")
    if len(ids) != len(set(ids)):
        raise ValidationError("an entity cannot repeat a chain id")
    return ids


def _validate_msa(msa: Any, sequence: str) -> None:
    if not isinstance(msa, Mapping) or not isinstance(msa.get("sequences"), list):
        raise ValidationError("MSA must use the SDK state shape with a sequences list")
    _reject_unknown_fields(msa, {"sequences", "headers", "deletions"}, "MSA")
    rows = msa["sequences"]
    if not rows or any(not isinstance(row, str) or not row for row in rows):
        raise ValidationError("MSA sequences must be non-empty strings")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValidationError("all MSA rows must have the same aligned length")
    if any(re.search(r"[^A-Za-z.\-]", row) for row in rows):
        raise ValidationError("MSA rows contain unsupported alignment symbols")
    query = rows[0].replace("-", "").replace(".", "").upper()
    if query != sequence:
        raise ValidationError("the ungapped MSA query must match its chain sequence")
    headers = msa.get("headers")
    if headers is not None and (
        not isinstance(headers, list)
        or len(headers) != len(rows)
        or any(not isinstance(header, str) for header in headers)
    ):
        raise ValidationError("MSA headers must be a string list matching MSA depth")
    deletions = msa.get("deletions")
    if deletions is not None:
        if (
            not isinstance(deletions, list)
            or len(deletions) != len(rows)
            or any(not isinstance(row, list) or len(row) != width for row in deletions)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for row in deletions
                for value in row
            )
        ):
            raise ValidationError(
                "MSA deletions must be a nonnegative finite numeric matrix matching the MSA"
            )


def _validate_modifications(entity: Mapping[str, Any], sequence_length: int) -> None:
    modifications = entity.get("modifications")
    if modifications is None:
        return
    if not isinstance(modifications, list):
        raise ValidationError("modifications must be a list")
    for modification in modifications:
        if not isinstance(modification, Mapping):
            raise ValidationError("each modification must be an object")
        _reject_unknown_fields(modification, {"position", "ccd"}, "modification")
        position = modification.get("position")
        ccd = modification.get("ccd")
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValidationError("modification position must be a zero-based integer")
        if not 0 <= position < sequence_length:
            raise ValidationError("modification position is outside its sequence")
        if not isinstance(ccd, str) or not ccd.strip():
            raise ValidationError("modification ccd must be a non-empty string")


def _nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{context} must be a nonnegative integer")
    return value


def _validate_residue_reference(
    chain_id: Any,
    residue_index: Any,
    *,
    chain_lengths: Mapping[str, int | None],
    context: str,
) -> tuple[str, int]:
    if not isinstance(chain_id, str) or chain_id not in chain_lengths:
        raise ValidationError(f"{context} references an unknown chain id")
    index = _nonnegative_int(residue_index, f"{context} residue index")
    length = chain_lengths[chain_id]
    if length is not None and index >= length:
        raise ValidationError(f"{context} residue index is outside chain {chain_id}")
    return chain_id, index


def _validate_pocket(
    value: Any, *, chain_lengths: Mapping[str, int | None]
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValidationError("pocket conditioning must be an object")
    _reject_unknown_fields(value, {"binder_chain_id", "contacts"}, "pocket")
    binder_chain_id = value.get("binder_chain_id")
    if not isinstance(binder_chain_id, str) or binder_chain_id not in chain_lengths:
        raise ValidationError("pocket binder_chain_id must reference an input chain")
    contacts = value.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        raise ValidationError("pocket contacts must be a non-empty list")
    for contact in contacts:
        if not isinstance(contact, (list, tuple)) or len(contact) != 2:
            raise ValidationError("each pocket contact must be [chain_id, residue_index]")
        _validate_residue_reference(
            contact[0],
            contact[1],
            chain_lengths=chain_lengths,
            context="pocket contact",
        )


def _validate_distogram_conditioning(
    value: Any, *, chain_lengths: Mapping[str, int | None]
) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not value:
        raise ValidationError("distogram_conditioning must be a non-empty list")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValidationError("each distogram conditioning entry must be an object")
        _reject_unknown_fields(item, {"chain_id", "distogram"}, "distogram conditioning")
        chain_id = item.get("chain_id")
        if not isinstance(chain_id, str) or chain_id not in chain_lengths:
            raise ValidationError("distogram conditioning references an unknown chain id")
        if chain_id in seen:
            raise ValidationError("distogram conditioning repeats a chain id")
        seen.add(chain_id)
        length = chain_lengths[chain_id]
        if length is None:
            raise ValidationError("distogram conditioning requires a sequence-based chain")
        matrix = item.get("distogram")
        if (
            not isinstance(matrix, list)
            or len(matrix) != length
            or any(not isinstance(row, list) or len(row) != length for row in matrix)
            or any(
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or number < 0
                for row in matrix
                for number in row
            )
        ):
            raise ValidationError(
                f"distogram for chain {chain_id} must be a finite nonnegative {length}x{length} matrix"
            )


def _validate_covalent_bonds(
    value: Any, *, chain_lengths: Mapping[str, int | None]
) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not value:
        raise ValidationError("covalent_bonds must be a non-empty list")
    required = {
        "chain_id1",
        "res_idx1",
        "atom_idx1",
        "chain_id2",
        "res_idx2",
        "atom_idx2",
    }
    for bond in value:
        if not isinstance(bond, Mapping):
            raise ValidationError("each covalent bond must be an object")
        _reject_unknown_fields(bond, required, "covalent bond")
        missing = sorted(required - set(bond))
        if missing:
            raise ValidationError(f"covalent bond is missing: {', '.join(missing)}")
        _validate_residue_reference(
            bond["chain_id1"],
            bond["res_idx1"],
            chain_lengths=chain_lengths,
            context="covalent bond endpoint 1",
        )
        _validate_residue_reference(
            bond["chain_id2"],
            bond["res_idx2"],
            chain_lengths=chain_lengths,
            context="covalent bond endpoint 2",
        )
        _nonnegative_int(bond["atom_idx1"], "covalent bond endpoint 1 atom index")
        _nonnegative_int(bond["atom_idx2"], "covalent bond endpoint 2 atom index")


def validate_fold_input(
    payload: Mapping[str, Any],
    *,
    model: str,
    require_msa: bool = False,
) -> dict[str, Any]:
    """Validate the SDK's serialized all-atom input shape.

    This intentionally validates only stable, documented fields. It does not
    invent a universal sequence-length cap for ESMFold2.
    """

    if model not in {*ESMFOLD2_MANAGED_MODELS, *ESMFOLD2_HF_MODELS}:
        raise ValidationError(f"unsupported ESMFold2 model: {model}")
    if not isinstance(payload, Mapping):
        raise ValidationError("fold input must be an object")
    _reject_unknown_fields(
        payload,
        {"sequences", "pocket", "distogram_conditioning", "covalent_bonds"},
        "fold input",
    )
    entities = payload.get("sequences")
    if not isinstance(entities, list) or not entities:
        raise ValidationError("fold input requires a non-empty sequences list")

    seen_ids: set[str] = set()
    chain_lengths: dict[str, int | None] = {}
    has_msa = False
    normalized_entities: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, Mapping):
            raise ValidationError("each fold entity must be an object")
        entity_type = entity.get("type")
        if entity_type not in {"protein", "dna", "rna", "ligand"}:
            raise ValidationError(f"unsupported fold modality: {entity_type!r}")
        entity_allowed = {
            "ligand": {"type", "id", "smiles", "ccd"},
            "dna": {"type", "id", "sequence", "modifications"},
            "protein": {"type", "id", "sequence", "modifications", "msa"},
            "rna": {"type", "id", "sequence", "modifications", "msa"},
        }[entity_type]
        _reject_unknown_fields(entity, entity_allowed, f"{entity_type} entity")
        ids = _validate_chain_ids(entity.get("id"))
        duplicates = seen_ids.intersection(ids)
        if duplicates:
            raise ValidationError(f"duplicate chain id: {sorted(duplicates)[0]}")
        seen_ids.update(ids)

        copy = dict(entity)
        if entity_type == "ligand":
            smiles = entity.get("smiles")
            ccd = entity.get("ccd")
            has_smiles = isinstance(smiles, str) and bool(smiles.strip())
            has_ccd = isinstance(ccd, list) and bool(ccd) and all(
                isinstance(item, str) and item.strip() for item in ccd
            )
            if has_smiles == has_ccd:
                raise ValidationError("ligand requires exactly one of non-empty smiles or ccd")
            if entity.get("msa") is not None:
                raise ValidationError("ligands do not support MSA input")
            ligand_length = len(ccd) if has_ccd else None
            for chain_id in ids:
                chain_lengths[chain_id] = ligand_length
        else:
            sequence = normalize_sequence(entity.get("sequence", ""))
            alphabet = {
                "protein": PROTEIN_ALPHABET,
                "dna": DNA_ALPHABET,
                "rna": RNA_ALPHABET,
            }[entity_type]
            _validate_alphabet(sequence, alphabet, entity_type)
            copy["sequence"] = sequence
            for chain_id in ids:
                chain_lengths[chain_id] = len(sequence)
            _validate_modifications(entity, len(sequence))
            msa = entity.get("msa")
            if msa is not None:
                if entity_type not in {"protein", "rna"}:
                    raise ValidationError(f"{entity_type} does not support MSA input")
                _validate_msa(msa, sequence)
                has_msa = True
        normalized_entities.append(copy)

    _validate_pocket(payload.get("pocket"), chain_lengths=chain_lengths)
    _validate_distogram_conditioning(
        payload.get("distogram_conditioning"), chain_lengths=chain_lengths
    )
    _validate_covalent_bonds(
        payload.get("covalent_bonds"), chain_lengths=chain_lengths
    )

    is_fast = model in {"esmfold2-fast-2026-05", "biohub/ESMFold2-Fast"}
    if is_fast and has_msa:
        raise ValidationError("ESMFold2-Fast is single-sequence only and does not accept MSA conditioning")
    if require_msa and not has_msa:
        raise ValidationError("this full-model workflow requires an MSA, but none was provided")

    result = dict(payload)
    result["sequences"] = normalized_entities
    return result


def validate_esmc_model(model: str) -> str:
    if model not in ESMC_MANAGED_MODELS:
        raise ValidationError(f"unsupported managed ESMC model: {model}")
    return model
