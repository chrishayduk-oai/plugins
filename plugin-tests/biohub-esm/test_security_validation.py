from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.errors import ValidationError
from biohub_esm_lib.security import credential_preflight, redact
from biohub_esm_lib.validation import (
    sequence_md5,
    validate_atlas_search_sequence,
    validate_batch_hashes,
    validate_esmc_sequence,
    validate_fold_input,
    validate_hosted_fold_config,
)


class SecurityTests(unittest.TestCase):
    def test_preflight_reports_presence_only(self) -> None:
        env = {
            "ESM_API_KEY": "esm-super-secret",
            "MODAL_TOKEN_ID": "ak-secret",
            "MODAL_TOKEN_SECRET": "as-secret",
            "HF_TOKEN": "hf-secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            result = credential_preflight(env, home=Path(directory))
        rendered = repr(result)
        self.assertEqual(result["biohub_managed"]["status"], "configured")
        self.assertEqual(result["modal"]["source"], "environment")
        for secret in env.values():
            self.assertNotIn(secret, rendered)

    def test_modal_profile_presence_is_enough(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".modal.toml").touch()
            result = credential_preflight({}, home=home)
        self.assertEqual(result["modal"], {"status": "configured", "source": "profile"})

    def test_recursive_redaction(self) -> None:
        env = {"ESM_API_KEY": "value-12345"}
        value = {
            "Authorization": "Bearer abcdef123",
            "nested": ["ESM_API_KEY=value-12345", {"token_secret": "visible"}],
            "message": "provider echoed value-12345",
        }
        rendered = repr(redact(value, env))
        self.assertNotIn("abcdef123", rendered)
        self.assertNotIn("value-12345", rendered)
        self.assertNotIn("visible", rendered)

    def test_presigned_url_credentials_are_redacted(self) -> None:
        value = {
            "url": "https://example.test/file?X-Amz-Credential=abc&X-Amz-Signature=def"
        }
        rendered = repr(redact(value, {}))
        self.assertNotIn("abc", rendered)
        self.assertNotIn("def", rendered)


class ValidationTests(unittest.TestCase):
    def test_esmc_and_atlas_limits(self) -> None:
        self.assertEqual(len(validate_esmc_sequence("A" * 2046)), 2046)
        with self.assertRaises(ValidationError):
            validate_esmc_sequence("A" * 2047)
        self.assertEqual(len(validate_atlas_search_sequence("A" * 800)), 800)
        with self.assertRaises(ValidationError):
            validate_atlas_search_sequence("A" * 801)

    def test_fasta_and_md5_are_deterministic(self) -> None:
        self.assertEqual(sequence_md5(">x\nACD\nEFG\n"), sequence_md5("ACDEFG"))
        with self.assertRaises(ValidationError):
            validate_esmc_sequence("ACD*EF")
        with self.assertRaisesRegex(ValidationError, "multiple headers"):
            validate_esmc_sequence(">one\nACD\n>two\nEFG")

    def test_batch_deduplicates_and_caps(self) -> None:
        digest = "a" * 32
        self.assertEqual(validate_batch_hashes([digest, digest]), [digest])
        with self.assertRaises(ValidationError):
            validate_batch_hashes([f"{index:032x}" for index in range(501)])
        with self.assertRaisesRegex(ValidationError, "protein hash"):
            validate_batch_hashes([3])  # type: ignore[list-item]

    def test_hosted_bounds_and_unknown_parameters(self) -> None:
        config = validate_hosted_fold_config(
            {"num_loops": 20, "num_sampling_steps": 100, "lm_dropout": 0.3, "msa_max_depth": None}
        )
        self.assertEqual(config["num_loops"], 20)
        for invalid in (
            {"num_loops": 21},
            {"num_sampling_steps": 0},
            {"lm_dropout": 1.1},
            {"unknown": True},
        ):
            with self.assertRaises(ValidationError):
                validate_hosted_fold_config(invalid)

    def test_complex_modalities_and_modifications(self) -> None:
        payload = {
            "sequences": [
                {"type": "protein", "id": "A", "sequence": "MKT"},
                {"type": "dna", "id": "B", "sequence": "ACGT", "modifications": [{"position": 2, "ccd": "C36"}]},
                {"type": "rna", "id": "R", "sequence": "ACGU"},
                {"type": "ligand", "id": "L", "ccd": ["SAH"]},
            ]
        }
        result = validate_fold_input(payload, model="esmfold2-2026-05")
        self.assertEqual(len(result["sequences"]), 4)

    def test_sdk_conditioning_shapes_are_validated(self) -> None:
        valid = {
            "sequences": [
                {"type": "protein", "id": "A", "sequence": "MKT"},
                {"type": "protein", "id": "B", "sequence": "MRA"},
            ],
            "pocket": {"binder_chain_id": "A", "contacts": [["B", 2]]},
            "distogram_conditioning": [
                {
                    "chain_id": "A",
                    "distogram": [[0, 1, 2], [1, 0, 1], [2, 1, 0]],
                }
            ],
            "covalent_bonds": [
                {
                    "chain_id1": "A",
                    "res_idx1": 0,
                    "atom_idx1": 1,
                    "chain_id2": "B",
                    "res_idx2": 2,
                    "atom_idx2": 3,
                }
            ],
        }
        result = validate_fold_input(valid, model="esmfold2-2026-05")
        self.assertEqual(result["pocket"]["binder_chain_id"], "A")

    def test_unknown_and_malformed_conditioning_fields_are_rejected(self) -> None:
        base = {"sequences": [{"type": "protein", "id": "A", "sequence": "MKT"}]}
        cases = [
            {**base, "typo": True},
            {
                "sequences": [
                    {"type": "protein", "id": "A", "sequence": "MKT", "smiles": "CC"}
                ]
            },
            {**base, "covalent_bonds": "oops"},
            {
                **base,
                "pocket": {"binder_chain_id": "A", "contacts": [["missing", 0]]},
            },
            {
                **base,
                "distogram_conditioning": [
                    {"chain_id": "A", "distogram": [[0, 1], [1, 0]]}
                ],
            },
            {
                **base,
                "covalent_bonds": [
                    {
                        "chain_id1": "A",
                        "res_idx1": 3,
                        "atom_idx1": 0,
                        "chain_id2": "A",
                        "res_idx2": 0,
                        "atom_idx2": 0,
                    }
                ],
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                validate_fold_input(payload, model="esmfold2-2026-05")

    def test_fast_rejects_msa_and_required_msa_is_enforced(self) -> None:
        payload = {
            "sequences": [
                {"type": "protein", "id": "A", "sequence": "MKT", "msa": {"sequences": ["MKT", "MRT"]}}
            ]
        }
        with self.assertRaisesRegex(ValidationError, "single-sequence"):
            validate_fold_input(payload, model="esmfold2-fast-2026-05")
        with self.assertRaisesRegex(ValidationError, "requires an MSA"):
            validate_fold_input(
                {"sequences": [{"type": "protein", "id": "A", "sequence": "MKT"}]},
                model="esmfold2-2026-05",
                require_msa=True,
            )

    def test_msa_query_shape_and_headers_are_validated(self) -> None:
        valid = {
            "sequences": [
                {
                    "type": "protein",
                    "id": "A",
                    "sequence": "MKT",
                    "msa": {
                        "sequences": ["M-KT", "MRKT"],
                        "headers": ["query", "hit"],
                    },
                }
            ]
        }
        validate_fold_input(valid, model="esmfold2-2026-05", require_msa=True)
        invalid_msas = (
            {"sequences": ["MKT", "MK-T"]},
            {"sequences": ["MRT", "MKT"]},
            {"sequences": ["MKT", "MRT"], "headers": ["query"]},
        )
        for msa in invalid_msas:
            payload = {
                "sequences": [
                    {"type": "protein", "id": "A", "sequence": "MKT", "msa": msa}
                ]
            }
            with self.assertRaises(ValidationError):
                validate_fold_input(payload, model="esmfold2-2026-05")

    def test_malformed_entity_rejected(self) -> None:
        cases = [
            {"sequences": [{"type": "ligand", "id": "L", "smiles": "CC", "ccd": ["ATP"]}]},
            {
                "sequences": [
                    {"type": "protein", "id": "A", "sequence": "MKT"},
                    {"type": "dna", "id": "A", "sequence": "ACGT"},
                ]
            },
            {"sequences": [{"type": "protein", "id": ["A", "A"], "sequence": "MKT"}]},
            {
                "sequences": [
                    {
                        "type": "protein",
                        "id": "A",
                        "sequence": "MKT",
                        "modifications": [{"position": 3, "ccd": "SEP"}],
                    }
                ]
            },
        ]
        for payload in cases:
            with self.assertRaises(ValidationError):
                validate_fold_input(payload, model="esmfold2-2026-05")


if __name__ == "__main__":
    unittest.main()
