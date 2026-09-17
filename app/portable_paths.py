from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

RUNTIME_TOKEN = "@runtime/"


def absolute_without_resolving(value: str | Path) -> Path:
    """Return an absolute path without resolving symlinks or Windows 8.3 aliases."""
    path = Path(value).expanduser()
    return Path(os.path.abspath(os.fspath(path)))


def is_runtime_token(value: object) -> bool:
    return str(value or "").strip().startswith(RUNTIME_TOKEN)


def decode_runtime_path(value: object, runtime_root: str | Path) -> Path | None:
    """Decode ``@runtime/...`` against the *current* application folder.

    The function intentionally does not call ``resolve()``.  That means the
    spelling of the current runtime path is preserved even when Windows exposes
    the same profile through a long Unicode name and an 8.3 alias.
    """
    text = str(value or "").strip()
    if not text:
        return None

    if not text.startswith(RUNTIME_TOKEN):
        return absolute_without_resolving(text)

    relative_text = text[len(RUNTIME_TOKEN):]
    parts = [part for part in relative_text.replace("\\", "/").split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return absolute_without_resolving(Path(runtime_root).joinpath(*parts))


def encode_runtime_path(value: object, runtime_root: str | Path) -> str:
    """Serialize a path under ``runtime_root`` as ``@runtime/...``.

    Paths outside the portable folder remain absolute.  Existing tokens are
    normalized and preserved, which keeps history/backup snapshots portable.
    """
    text = str(value or "").strip()
    if not text:
        return ""

    root = absolute_without_resolving(runtime_root)
    if text.startswith(RUNTIME_TOKEN):
        decoded = decode_runtime_path(text, root)
        if decoded is None:
            return ""
        path = decoded
    else:
        path = absolute_without_resolving(text)

    try:
        relative = path.relative_to(root)
    except ValueError:
        # Windows may expose the same directory through an 8.3 alias while a
        # file API returns the long Unicode spelling.  Resolve only for the
        # *comparison*; the persisted value remains relative and portable.
        try:
            relative = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return str(path)
    return RUNTIME_TOKEN + relative.as_posix()


def portable_basename(value: object) -> str:
    """Return a filename from native, tokenized, or foreign Windows paths."""
    text = str(value or "")
    if not text:
        return ""
    if "\\" in text:
        return PureWindowsPath(text).name
    return Path(text).name
