from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "plugins" / "biohub-esm"
SKILLS = {
    "biohub-esm",
    "biohub-esm-setup",
    "esmc",
    "esmfold2",
    "esm-atlas",
    "esmfold2-binder-design",
}


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


class ManifestTests(unittest.TestCase):
    def test_plugin_manifest(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], ROOT.name)
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["skills"], "./skills")
        self.assertEqual(manifest["interface"]["category"], "Education & Research")
        self.assertLessEqual(len(manifest["interface"]["defaultPrompt"]), 3)
        self.assertTrue(all(len(prompt) <= 128 for prompt in manifest["interface"]["defaultPrompt"]))
        self.assertNotIn("TODO", json.dumps(manifest))
        for field in ("composerIcon", "logo"):
            self.assertTrue((ROOT / manifest["interface"][field]).is_file())

    def test_skill_frontmatter_and_agent_metadata(self) -> None:
        actual = {path.name for path in (ROOT / "skills").iterdir() if path.is_dir()}
        self.assertEqual(actual, SKILLS)
        for name in SKILLS:
            skill = ROOT / "skills" / name / "SKILL.md"
            metadata = ROOT / "skills" / name / "agents" / "openai.yaml"
            parsed = frontmatter(skill)
            self.assertEqual(parsed["name"], name)
            self.assertIn("Use", parsed["description"])
            self.assertLessEqual(len(parsed["description"]), 1024)
            agent_text = metadata.read_text()
            self.assertIn("display_name:", agent_text)
            self.assertIn("short_description:", agent_text)
            self.assertIn("default_prompt:", agent_text)
            expected_policy = "true" if name == "biohub-esm" else "false"
            self.assertIn(
                f"allow_implicit_invocation: {expected_policy}", agent_text
            )
            self.assertNotIn("TODO", skill.read_text() + agent_text)

    def test_marketplace_registries(self) -> None:
        for filename in ("marketplace.json", "api_marketplace.json"):
            registry = json.loads((REPO / ".agents" / "plugins" / filename).read_text())
            entries = [entry for entry in registry["plugins"] if entry["name"] == "biohub-esm"]
            self.assertEqual(len(entries), 1, filename)
            entry = entries[0]
            self.assertEqual(entry["source"]["path"], "./plugins/biohub-esm")
            self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
            self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")
            self.assertEqual(entry["category"], "Education & Research")

    def test_all_required_primary_sources_are_cited(self) -> None:
        required = {
            "https://biohub.ai/esm/protein",
            "https://biohub.ai/esm/protein/get-started",
            "https://biohub.ai/models/esmc",
            "https://biohub.ai/models/esmfold2",
            "https://biohub.ai/api-reference/logits",
            "https://biohub.ai/esm/protein/atlas/api-docs/overview.html",
            "https://biohub.ai/esm/protein/atlas/api-docs/api_reference.html",
            "https://github.com/Biohub/esm",
            "https://huggingface.co/biohub/ESMC-6B",
            "https://huggingface.co/biohub/ESMFold2",
            "https://modal.com/docs/examples/esmfold2",
            "https://modal.com/docs/examples/esmfold2_binder_design",
            "https://modal.com/docs/sdk/py/latest/modal.config",
            "https://modal.com/docs/guide/scale",
            "https://www.biorxiv.org/content/10.64898/2026.06.03.729735v1",
        }
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [*ROOT.rglob("*.md"), *ROOT.rglob("*.json")]
        )
        self.assertEqual({url for url in required if url not in corpus}, set())
        self.assertIn("ef32577f55da19a4989cd7b22e004dc43a4998cb", corpus)


if __name__ == "__main__":
    unittest.main()
