import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V1170VisualPolishTests(unittest.TestCase):
    def test_version_is_1170(self):
        text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('__version__ = "1.17.0"', text)

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
