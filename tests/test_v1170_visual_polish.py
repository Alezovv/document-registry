import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V1170VisualPolishTests(unittest.TestCase):
    def test_current_version_matches_release(self):
        app_text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
        build_text = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        import re
        app_match = re.search(r'__version__\s*=\s*"([^"]+)"', app_text)
        build_match = re.search(r'\[string\]\$Version\s*=\s*"([^"]+)"', build_text)
        self.assertIsNotNone(app_match)
        self.assertIsNotNone(build_match)
        self.assertEqual(app_match.group(1), build_match.group(1))

    def test_primary_registry_button_does_not_resize_on_hover(self):
        text = (ROOT / "app" / "ui" / "registry_page.py").read_text(encoding="utf-8")
        block = text.split('add_button.setStyleSheet(', 1)[1].split('add_button.clicked', 1)[0]
        self.assertNotIn('padding-left: 22px', block)
        self.assertNotIn('padding-right: 22px', block)
        self.assertIn('border-color: {PRIMARY_HOVER}', block)

    def test_sidebar_has_distinct_active_accent(self):
        text = (ROOT / "app" / "ui" / "navigation_sidebar.py").read_text(encoding="utf-8")
        self.assertIn('border-left: 3px solid #60A5FA', text)
        self.assertIn('background-color: #183A70', text)

if __name__ == "__main__":
    unittest.main()
