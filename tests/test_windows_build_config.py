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
        self.assertIn("DocumentRegistryUpdater", text)
        self.assertIn("updater_main.py", text)
        self.assertIn('collect_data_files("paddleocr"', text)
        self.assertIn('collect_data_files("paddlex"', text)
        self.assertIn('collect_submodules("paddlex.inference"', text)
        self.assertIn('copy_metadata("paddlex"', text)
        self.assertIn('collect_dynamic_libs("paddle"', text)
        self.assertIn("BASE_DEP_SPECS", text)
        self.assertIn("importlib_metadata.distributions", text)

    def test_build_script_creates_clean_portable_layout_and_runs_frozen_self_check(self):
        text = (PACKAGING / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Remove-Item", text)
        self.assertIn('Join-Path $Root "models"', text)
        self.assertIn("portable.flag", text)
        self.assertIn("--self-check", text)
        self.assertIn("--ocr-self-check", text)
        self.assertIn("Frozen OCR self-check", text)
        self.assertIn("frozen_ocr_self_check.json", text)
        self.assertIn("--self-check-output", text)
        self.assertIn("ConvertFrom-Json", text)
        self.assertIn("Start-Process", text)
        self.assertIn("-PassThru", text)
        self.assertIn("-Wait", text)
        self.assertIn("New-ReleaseZipWithRetry", text)
        self.assertIn("Wait-ReadableFile", text)
        self.assertIn("[System.IO.FileShare]::None", text)
        self.assertIn("DocumentRegistry_zip_", text)
        self.assertIn("CreateFromDirectory", text)
        self.assertIn("$Root = Split-Path -Parent $PackagingDir", text)
        self.assertIn('Join-Path $PackagingDir "DocumentRegistry.spec"', text)
        self.assertIn('[cleanup] Removing temporary build and dist folders', text)
        self.assertIn('foreach ($TempDir in @($BuildDir, $DistDir))', text)
        self.assertIn("-PublishExisting", text)
        self.assertIn("manifest.json", text)
        self.assertIn("DocumentRegistry_update.zip", text)


if __name__ == "__main__":
    unittest.main()
