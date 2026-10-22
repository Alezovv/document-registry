from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from app.models import Request

CACHE_FORMAT_VERSION = 1
OCR_ALGO_VERSION = "1.30.0"
PARSE_RULES_REVISION = "1.30.0"
DEFAULT_MAX_ENTRIES = 400
DEFAULT_MAX_BYTES = 96 * 1024 * 1024

_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, threading.RLock] = {}


def _lock_for_cache_dir(cache_dir: Path) -> threading.RLock:
    key = os.path.normcase(str(cache_dir.resolve(strict=False)))
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _CACHE_LOCKS[key] = lock
        return lock


class ParserCache:
    """Small local cache for OCR payloads and parser results.

    Cache entries are keyed by the PDF content hash, so moving the portable
    application to another PC or changing the absolute path does not invalidate
    useful OCR data.  The original PDF is never copied into the cache.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(20, int(max_entries))
        self.max_bytes = max(8 * 1024 * 1024, int(max_bytes))
        self._writes_since_prune = 0
        # Instances targeting the same directory share a lock, so simultaneous
        # OCR/parser workers cannot overwrite each other's read-modify-write.
        self._write_lock = _lock_for_cache_dir(self.cache_dir)

    @staticmethod
    def content_hash(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _entry_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"

    def _read_key(self, cache_key: str) -> dict[str, Any] | None:
        path = self._entry_path(cache_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("format_version") != CACHE_FORMAT_VERSION:
            return None
        return payload

    def lookup(
        self, pdf_path: str | Path, *, cache_key: str | None = None
    ) -> tuple[str, dict[str, Any] | None]:
        key = cache_key or self.content_hash(pdf_path)
        return key, self._read_key(key)

    def load_ocr(
        self, pdf_path: str | Path, *, cache_key: str | None = None
    ) -> tuple[str, dict[str, Any] | None]:
        key, entry = self.lookup(pdf_path, cache_key=cache_key)
        if not entry:
            return key, None
        ocr = entry.get("ocr")
        if (
            not isinstance(ocr, dict)
            or ocr.get("algo_version") != OCR_ALGO_VERSION
            or not str(ocr.get("text", "")).strip()
        ):
            return key, None
        copied = {
            "text": str(ocr.get("text", "")),
            "blocks": list(ocr.get("blocks") or []),
            "_cache_key": key,
        }
        if "_preparsed" in ocr and isinstance(ocr["_preparsed"], dict):
            copied["_preparsed"] = dict(ocr["_preparsed"])
        return key, copied

    def load_parse(
        self,
        pdf_path: str | Path,
        *,
        parser_version: str,
        cache_key: str | None = None,
    ) -> tuple[Request, dict[str, Any]] | None:
        key = cache_key or self.content_hash(pdf_path)
        entry = self._read_key(key)
        if (
            not entry
            or entry.get("parser_version") != parser_version
            or entry.get("rules_revision") != PARSE_RULES_REVISION
        ):
            return None
        data = entry.get("request")
        if not isinstance(data, dict):
            return None
        allowed = {name for name in Request.__dataclass_fields__}
        request = Request(**{k: v for k, v in data.items() if k in allowed})
        diagnostics = entry.get("diagnostics")
        return request, diagnostics if isinstance(diagnostics, dict) else {}

    def load_regions(self, pdf_path: str | Path) -> dict[str, list[dict[str, Any]]]:
        try:
            _key, entry = self.lookup(pdf_path)
        except OSError:
            return {}
        if not entry:
            return {}
        diagnostics = entry.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return {}
        regions = diagnostics.get("field_regions")
        return regions if isinstance(regions, dict) else {}

    def save_ocr(
        self,
        pdf_path: str | Path,
        ocr_data: dict[str, Any],
        *,
        cache_key: str | None = None,
    ) -> str:
        key = cache_key or self.content_hash(pdf_path)
        with self._write_lock:
            entry = self._read_key(key) or {"format_version": CACHE_FORMAT_VERSION}
            entry["pdf_size"] = self._safe_size(pdf_path)
            ocr_payload = {
                "algo_version": OCR_ALGO_VERSION,
                "text": str(ocr_data.get("text", "")),
                "blocks": list(ocr_data.get("blocks") or []),
            }
            if "_preparsed" in ocr_data and isinstance(ocr_data["_preparsed"], dict):
                ocr_payload["_preparsed"] = dict(ocr_data["_preparsed"])
            entry["ocr"] = ocr_payload
            self._write_key(key, entry)
        return key

    def save_parse(
        self,
        pdf_path: str | Path,
        request: Request,
        diagnostics: dict[str, Any],
        *,
        parser_version: str,
        cache_key: str | None = None,
    ) -> str:
        key = cache_key or self.content_hash(pdf_path)
        with self._write_lock:
            entry = self._read_key(key) or {"format_version": CACHE_FORMAT_VERSION}
            entry["pdf_size"] = self._safe_size(pdf_path)
            entry["parser_version"] = parser_version
            entry["rules_revision"] = PARSE_RULES_REVISION
            # Cache only fields represented by Request. Runtime ids/source paths are
            # deliberately excluded from parsed inference.
            request_data = asdict(request)
            request_data["id"] = None
            request_data["source_document"] = ""
            entry["request"] = request_data
            entry["diagnostics"] = diagnostics
            self._write_key(key, entry)
        return key

    def _write_key(self, cache_key: str, payload: dict[str, Any]) -> None:
        target = self._entry_path(cache_key)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=self._json_default)
        temp_name: str | None = None
        temp_fd: int | None = None
        with self._write_lock:
            try:
                temp_fd, temp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp", dir=self.cache_dir
                )
                with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as stream:
                    temp_fd = None  # fdopen now owns the descriptor
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, target)
                temp_name = None
                self._writes_since_prune += 1
                custom_limits = (
                    self.max_entries < DEFAULT_MAX_ENTRIES
                    or self.max_bytes < DEFAULT_MAX_BYTES
                )
                if custom_limits or self._writes_since_prune >= 20:
                    self._writes_since_prune = 0
                    self.prune()
            finally:
                if temp_fd is not None:
                    try:
                        os.close(temp_fd)
                    except OSError:
                        pass
                if temp_name is not None:
                    try:
                        Path(temp_name).unlink(missing_ok=True)
                    except OSError:
                        pass

    def prune(self) -> None:
        with self._write_lock:
            # Crash leftovers use hidden unique temp names. Remove only files
            # older than 24 hours so a concurrent writer's active temp file is
            # never mistaken for an orphan.
            stale_before_ns = time.time_ns() - 24 * 60 * 60 * 1_000_000_000
            for temp_path in self.cache_dir.glob(".*.json.*.tmp"):
                try:
                    if temp_path.is_file() and temp_path.stat().st_mtime_ns < stale_before_ns:
                        temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

            entries = []
            for path in self.cache_dir.glob("*.json"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((stat.st_mtime_ns, stat.st_size, path))
            entries.sort(reverse=True)
            total = 0
            for index, (_mtime, size, path) in enumerate(entries):
                total += size
                if index >= self.max_entries or total > self.max_bytes:
                    try:
                        path.unlink()
                    except OSError:
                        pass

    @staticmethod
    def _safe_size(path: str | Path) -> int:
        try:
            return int(Path(path).stat().st_size)
        except OSError:
            return 0

    @staticmethod
    def _json_default(value):
        item = getattr(value, "item", None)
        if callable(item):
            return item()
        return str(value)
