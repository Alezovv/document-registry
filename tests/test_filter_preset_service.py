"""Overlay-update tombstone for the removed saved-filter feature.

Older installations may still contain this test after a ZIP is unpacked on top of
an existing project. Keeping this filename in current releases guarantees that
the obsolete test is overwritten instead of importing the removed service.
"""

import unittest
from pathlib import Path

from app.update_cleanup import cleanup_legacy_update_files


ROOT = Path(__file__).resolve().parents[1]


class RemovedFilterPresetFeatureTests(unittest.TestCase):
    def test_legacy_saved_filter_artifacts_are_cleaned(self):
        cleanup_legacy_update_files(ROOT)
        self.assertFalse((ROOT / "app" / "filter_preset_service.py").exists())
        self.assertFalse((ROOT / "data" / "filter_presets.json").exists())


if __name__ == "__main__":
    unittest.main()
