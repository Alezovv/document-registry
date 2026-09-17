from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


class WindowsBuildConfigTests(unittest.TestCase):
    def test_spec_embeds_icon_but_keeps_ocr_models_external(self):
        text = (PACKAGING / "DocumentRegistry.spec").read_text(encoding="utf-8")
        self.assertIn("app_icon.ico", text)
        self.assertNotIn("models", text.lower())
        self.assertIn("PACKAGING_DIR.parent", text)

    def test_build_script_creates_clean_portable_layout_and_runs_frozen_self_check(self):
        text = (PACKAGING / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Remove-Item", text)
        self.assertIn('Join-Path $Root "models"', text)
        self.assertIn("portable.flag", text)
        self.assertIn("--self-check", text)
        self.assertIn("$Root = Split-Path -Parent $PackagingDir", text)
        self.assertIn('Join-Path $PackagingDir "DocumentRegistry.spec"', text)


if __name__ == "__main__":
    unittest.main()
