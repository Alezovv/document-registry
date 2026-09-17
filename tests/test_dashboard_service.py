"""Overlay-update tombstone for the removed Overview/Dashboard feature.

Older installations may still contain this test after a ZIP is unpacked on top
of an existing project. This current file overwrites that legacy test and checks
that obsolete dashboard modules are removed by migration cleanup.
"""

import unittest
from pathlib import Path

from app.update_cleanup import cleanup_legacy_update_files

ROOT = Path(__file__).resolve().parents[1]


class RemovedDashboardFeatureTests(unittest.TestCase):
    def test_legacy_dashboard_artifacts_are_cleaned(self):
        cleanup_legacy_update_files(ROOT)
        self.assertFalse((ROOT / "app" / "dashboard_service.py").exists())
        self.assertFalse((ROOT / "app" / "ui" / "dashboard_page.py").exists())


if __name__ == "__main__":
    unittest.main()
