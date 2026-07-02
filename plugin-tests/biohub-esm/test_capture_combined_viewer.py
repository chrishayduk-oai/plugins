from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from capture_combined_viewer import browser_environment, capture
from biohub_esm_lib.errors import ValidationError


class CombinedViewerCaptureTests(unittest.TestCase):
    def test_browser_environment_excludes_scientific_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ESM_API_KEY": "secret",
                "HF_TOKEN": "secret",
                "MODAL_TOKEN_ID": "secret",
                "MODAL_TOKEN_SECRET": "secret",
            },
        ):
            env = browser_environment()
        for name in (
            "ESM_API_KEY",
            "HF_TOKEN",
            "MODAL_TOKEN_ID",
            "MODAL_TOKEN_SECRET",
        ):
            self.assertNotIn(name, env)

    def test_capture_requires_npx_before_launching_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "viewer.html").write_text("<html></html>")
            wrapper = root / "playwright_cli.sh"
            wrapper.write_text("#!/bin/sh\nexit 0\n")
            wrapper.chmod(0o755)
            with patch("capture_combined_viewer.shutil.which", return_value=None):
                with self.assertRaisesRegex(ValidationError, "npx"):
                    capture(
                        run_dir=root,
                        source_url="http://127.0.0.1:8000/viewer.html",
                        pwcli=wrapper,
                        width=1440,
                        height=900,
                        ready_timeout=1,
                        session="test",
                    )
            self.assertFalse((root / "screenshot.png").exists())
