from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from biohub_esm_lib.activation import select_skill


class ActivationTests(unittest.TestCase):
    def test_positive_and_adjacent_negative_prompts(self) -> None:
        cases = json.loads((TEST_ROOT / "activation_cases.json").read_text())
        for case in cases:
            with self.subTest(prompt=case["prompt"]):
                self.assertEqual(select_skill(case["prompt"]), case["expected"])

    def test_every_skill_has_positive_coverage(self) -> None:
        cases = json.loads((TEST_ROOT / "activation_cases.json").read_text())
        covered = {case["expected"] for case in cases if case["expected"]}
        self.assertEqual(
            covered,
            {
                "biohub-esm",
                "biohub-esm-setup",
                "esmc",
                "esmfold2",
                "esm-atlas",
                "esmfold2-binder-design",
            },
        )


if __name__ == "__main__":
    unittest.main()
