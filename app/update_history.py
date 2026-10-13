from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

HISTORY_FILE_NAME = "update_history.jsonl"
MAX_HISTORY_BYTES = 2 * 1024 * 1024
KEEP_LINES = 1200


@dataclass(frozen=True, slots=True)
class UpdateHistoryEntry:
    session_id: str
    version: str
    state: str
    stage: str
    message: str
    progress: int
    timestamp: str
    window_started: bool | None = None


def history_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / HISTORY_FILE_NAME


def append_update_history(data_dir: str | Path, payload: dict[str, Any]) -> None:
    """Append one updater event without ever blocking installation on logging errors."""
    path = history_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        _trim_history_if_needed(path)
    except OSError:
        return


def load_update_history(data_dir: str | Path, *, limit: int = 250) -> list[dict[str, Any]]:
    path = history_path(data_dir)
    safe_limit = max(1, min(int(limit), 2000))
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    entries: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            entries.append(item)
            if len(entries) >= safe_limit:
                break
    return entries


def _trim_history_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size <= MAX_HISTORY_BYTES:
            return
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        kept = lines[-KEEP_LINES:]
        temp = path.with_suffix(".jsonl.tmp")
        temp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(temp, path)
    except (OSError, UnicodeDecodeError):
        return
