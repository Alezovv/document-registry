from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile

APP_FOLDER_NAME = "DocumentRegistry"
ENV_HOME = "DOCUMENT_REGISTRY_HOME"
PORTABLE_MARKER = "portable.flag"


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def executable_dir() -> Path:
    """Return the directory containing the launched executable/project."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _source_root()


def resource_dir() -> Path:
    """Return the read-only resource root.

    PyInstaller one-file extracts bundled assets into ``sys._MEIPASS``.  User
    data must never be written there; it disappears when the process exits.
    """
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root).resolve()
    return _source_root()


def _can_write_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".registry_write_", dir=path, delete=True):
            pass
        return True
    except OSError:
        return False


def _fallback_runtime_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).expanduser() / APP_FOLDER_NAME
    return Path.home() / f".{APP_FOLDER_NAME.lower()}"


def runtime_dir() -> Path:
    """Return the writable application root.

    The application is portable by default: on a writable Desktop/folder/USB
    drive, ``data``, ``logs`` and ``backups`` live beside the executable.  If
    Windows denies writes (for example under Program Files), data transparently
    falls back to ``%LOCALAPPDATA%/DocumentRegistry``.

    ``DOCUMENT_REGISTRY_HOME`` can explicitly choose another root.  This also
    makes support/testing deterministic without depending on the current working
    directory.
    """
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        root = Path(override).expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        return root

    root = executable_dir()
    if _can_write_directory(root):
        return root.resolve(strict=False)

    fallback = _fallback_runtime_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve(strict=False)


@dataclass(frozen=True, slots=True)
class AppPaths:
    executable_dir: Path
    resources: Path
    runtime: Path
    data: Path
    documents: Path
    backups: Path
    logs: Path
    paddle_cache: Path
    assets: Path
    models: Path

    @property
    def database(self) -> Path:
        return self.data / "requests.db"

    @property
    def settings(self) -> Path:
        return self.data / "settings.json"

    @property
    def app_icon_png(self) -> Path:
        return self.assets / "app_icon.png"

    @property
    def app_icon_ico(self) -> Path:
        return self.assets / "app_icon.ico"


def _models_dir(exe_dir: Path, resources: Path, runtime: Path) -> Path:
    """Resolve OCR model files without depending on the installation location."""
    candidates = (
        exe_dir / "models",       # onedir / external model pack beside .exe
        runtime / "models",       # fallback runtime location
        resources / "models",     # bundled PyInstaller one-file resources
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve(strict=False)
    # Prefer bundled location for the error message on a clean frozen build.
    return (resources / "models").resolve(strict=False)


def get_app_paths() -> AppPaths:
    exe_dir = executable_dir()
    resources = resource_dir()
    runtime = runtime_dir()
    return AppPaths(
        executable_dir=exe_dir,
        resources=resources,
        runtime=runtime,
        data=runtime / "data",
        documents=runtime / "data" / "documents",
        backups=runtime / "backups",
        logs=runtime / "logs",
        paddle_cache=runtime / "data" / "paddle_cache",
        assets=resources / "assets",
        models=_models_dir(exe_dir, resources, runtime),
    )


def ensure_runtime_directories(paths: AppPaths | None = None) -> AppPaths:
    paths = paths or get_app_paths()
    for directory in (
        paths.runtime,
        paths.data,
        paths.documents,
        paths.backups,
        paths.logs,
        paths.paddle_cache,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths
