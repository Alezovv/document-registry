from __future__ import annotations

from pathlib import Path


def cleanup_legacy_update_files(root: Path) -> list[Path]:
    """Remove obsolete files that ZIP extraction cannot delete on update.

    Returns the paths successfully removed. Failures are intentionally ignored:
    stale files are harmless because current application code does not import them.
    """
    root = Path(root)
    obsolete = [
        root / "app" / "filter_preset_service.py",
        root / "data" / "filter_presets.json",
        root / "app" / "dashboard_service.py",
        root / "app" / "ui" / "dashboard_page.py",
    ]
    removed: list[Path] = []

    for path in obsolete:
        try:
            if path.exists():
                path.unlink()
                removed.append(path)
        except OSError:
            pass

    pycache = root / "app" / "__pycache__"
    if pycache.exists():
        for pattern in ("filter_preset_service*.pyc", "dashboard_service*.pyc"):
            for path in pycache.glob(pattern):
                try:
                    path.unlink()
                    removed.append(path)
                except OSError:
                    pass

    ui_pycache = root / "app" / "ui" / "__pycache__"
    if ui_pycache.exists():
        for path in ui_pycache.glob("dashboard_page*.pyc"):
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass

    return removed
