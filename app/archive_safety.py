"""Shared validation for ZIP archives extracted into application directories.

ZIP member names use POSIX separators by specification. Rejecting ambiguous and
Windows-invalid paths here makes extraction behavior consistent across the
Windows release and Linux/macOS test environments.
"""
from __future__ import annotations

from pathlib import Path
import re
import stat
import unicodedata
from typing import Iterable, Type


_DRIVE_PREFIX = re.compile(r"^[a-zA-Z]:")
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    # Windows also treats these superscript-digit variants as reserved names.
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}


def validate_archive_member_paths(
    members: Iterable[object],
    destination: str | Path,
    error_type: Type[Exception],
) -> list[Path]:
    """Validate every ZIP member before writing any member to disk.

    The returned target paths are aligned with ``members``. Validation rejects
    traversal, absolute/drive paths, symlinks, duplicate/case-colliding names,
    file-vs-directory conflicts, and names that have ambiguous behavior on
    Windows (the application's primary platform).
    """
    root = Path(destination).resolve(strict=False)
    materialized = list(members)
    targets: list[Path] = []
    keys: list[str] = []
    is_directory: list[bool] = []
    original_by_key: dict[str, str] = {}
    directory_by_key: dict[str, bool] = {}

    def fail(message: str) -> None:
        raise error_type(message)

    for member in materialized:
        raw_name = str(getattr(member, "filename", "") or "")
        if not raw_name or "\x00" in raw_name:
            fail("Архив содержит пустое или некорректное имя файла.")
        if "\\" in raw_name:
            fail(f"В архиве используется неоднозначный разделитель пути: {raw_name}")
        if raw_name.startswith("/") or _DRIVE_PREFIX.match(raw_name):
            fail(f"Архив содержит абсолютный путь: {raw_name}")

        entry_is_dir = bool(getattr(member, "is_dir", lambda: False)()) or raw_name.endswith("/")
        normalized_name = raw_name[:-1] if raw_name.endswith("/") else raw_name
        parts = normalized_name.split("/")
        if not normalized_name or any(part in ("", ".", "..") for part in parts):
            fail(f"В архиве обнаружен небезопасный путь: {raw_name}")

        for part in parts:
            if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith((" ", ".")):
                fail(f"Имя элемента архива несовместимо с Windows: {raw_name}")
            device_base = part.split(".", 1)[0].rstrip(" .").upper()
            if device_base in _WINDOWS_RESERVED_NAMES:
                fail(f"Имя элемента архива зарезервировано Windows: {raw_name}")

        mode = (int(getattr(member, "external_attr", 0)) >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            fail(f"Архив содержит символическую ссылку: {raw_name}")

        # ZIP paths are case-insensitive on the application's Windows target.
        # NFC normalization also catches visually identical names encoded with
        # different Unicode composition on filesystems that normalize names.
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if key in directory_by_key:
            fail(
                "Архив содержит повторяющиеся или конфликтующие имена: "
                f"{original_by_key[key]} и {raw_name}"
            )

        target = (root.joinpath(*parts)).resolve(strict=False)
        if target != root and root not in target.parents:
            fail(f"В архиве обнаружен небезопасный путь: {raw_name}")

        keys.append(key)
        is_directory.append(entry_is_dir)
        targets.append(target)
        original_by_key[key] = raw_name
        directory_by_key[key] = entry_is_dir

    # A regular file cannot be an ancestor of another archive member (e.g.
    # both "payload" and "payload/app.exe"). Check after building the map so
    # the result does not depend on the order entries appear in the ZIP.
    for key in keys:
        segments = key.split("/")
        for index in range(1, len(segments)):
            ancestor = "/".join(segments[:index])
            if ancestor in directory_by_key and not directory_by_key[ancestor]:
                fail(
                    "Элементы архива конфликтуют по пути: "
                    f"{original_by_key[ancestor]} и {original_by_key[key]}"
                )

    return targets
