from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_ROOT))

from combined_demo import (
    artifact_media_type,
    global_alignment_pairs,
    parse_ca_coordinates,
    run,
    viewer_html,
)
from biohub_esm_lib.errors import ValidationError


class CombinedDemoTests(unittest.TestCase):
    def test_live_demo_rejects_stale_output_before_credentials_or_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "viewer.html").write_text("stale")
            with self.assertRaisesRegex(ValidationError, "stale evidence"):
                run(root)

    def test_global_alignment_pairs_tracks_insertion(self) -> None:
        pairs = global_alignment_pairs("ACDE", "ACXDE")
        self.assertEqual(pairs, [(0, 0), (1, 1), (2, 3), (3, 4)])

    def test_parse_ca_coordinates_ignores_other_atoms_and_alt_duplicates(self) -> None:
        pdb = (
            "ATOM      1  N   ALA A   1       1.000   2.000   3.000  1.00  0.00           N  \n"
            "ATOM      2  CA  ALA A   1       2.000   3.000   4.000  1.00  0.00           C  \n"
            "ATOM      3  CA  ALA A   1       9.000   9.000   9.000  1.00  0.00           C  \n"
            "ATOM      4  CA  GLY A   2       5.000   6.000   7.000  1.00  0.00           C  \n"
        )
        self.assertEqual(parse_ca_coordinates(pdb), [(2.0, 3.0, 4.0), (5.0, 6.0, 7.0)])

    def test_viewer_escapes_remote_text_and_artifacts_have_media_types(self) -> None:
        summary = {
            "timestamp": "2026-07-01T00:00:00Z",
            "fold": {"mean_plddt": 0.8, "ptm": 0.7, "model": "model"},
            "hits": [
                {
                    "accession": "<script>alert(1)</script>",
                    "similarity": 0.9,
                    "rmsd": 1.0,
                    "aligned_positions": 3,
                }
            ],
            "features": [
                {"feature_index": 1, "label": "<b>x</b>", "summary": "<img src=x>"}
            ],
        }
        rendered = viewer_html(summary, ["hit-1-aligned.pdb"])
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertNotIn("<img src=x>", rendered)
        self.assertEqual(artifact_media_type(Path("result.cif")), "chemical/x-mmcif")


if __name__ == "__main__":
    unittest.main()
