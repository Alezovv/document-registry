from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app_paths import get_app_paths
from app.document_parser import DocumentParser, get_parser_cache_version
from app.ocr import OCR, OCRError
from app.parser_cache import ParserCache
from app.parser_correction_memory import ParserCorrectionMemory
from app.portable_paths import decode_runtime_path


def load_document_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, source_document
            FROM requests
            WHERE source_document IS NOT NULL AND source_document != ''
            ORDER BY id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare missing OCR/parser cache for reviewed registry documents. "
            "The database and PDFs are never modified."
        )
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum documents to inspect (default: 100)")
    parser.add_argument("--database", type=Path, default=None, help="SQLite database path")
    parser.add_argument("--cache", type=Path, default=None, help="Parser cache directory")
    parser.add_argument("--dry-run", action="store_true", help="Only report what is missing; do not run OCR")
    parser.add_argument(
        "--ocr-only",
        action="store_true",
        help="Prepare OCR cache only; skip parser result refresh",
    )
    args = parser.parse_args()

    paths = get_app_paths()
    db_path = args.database or paths.database
    cache_dir = args.cache or (paths.data / "parser_cache")
    rows = load_document_rows(db_path)
    if not rows:
        print(f"No requests with managed PDFs found in: {db_path}", file=sys.stderr)
        return 2

    cache = ParserCache(cache_dir)
    memory = ParserCorrectionMemory.for_data_dir(paths.data)
    parser_engine = None if args.ocr_only else DocumentParser(correction_memory=memory)
    ocr_engine: OCR | None = None

    inspected = 0
    existing = 0
    prepared = 0
    missing_pdf = 0
    failed = 0

    print("Gold-cache preparation")
    print("Database/PDFs are read-only. OCR runs only for documents missing OCR cache.")
    if args.dry_run:
        print("DRY RUN: no OCR/parser cache will be written.")
    print()

    for row in rows:
        if inspected >= max(1, args.limit):
            break
        inspected += 1
        request_id = int(row["id"])
        source = decode_runtime_path(row.get("source_document"), paths.runtime)
        if source is None or not source.is_file():
            missing_pdf += 1
            print(f"[{inspected:03d}] req={request_id:04d} missing PDF")
            continue

        try:
            cache_key, ocr_data = cache.load_ocr(source)
        except OSError as error:
            failed += 1
            print(f"[{inspected:03d}] req={request_id:04d} cache/hash error: {error}")
            continue

        if ocr_data is not None:
            existing += 1
            if parser_engine is not None and not args.dry_run:
                parsed = parser_engine.parse(ocr_data, file_name=source.name)
                cache.save_parse(
                    source,
                    parsed,
                    asdict(parser_engine.last_diagnostics),
                    parser_version=get_parser_cache_version(memory.cache_key()),
                    cache_key=cache_key,
                )
            print(f"[{inspected:03d}] req={request_id:04d} cache OK")
            continue

        if args.dry_run:
            print(f"[{inspected:03d}] req={request_id:04d} OCR missing")
            continue

        if ocr_engine is None:
            ocr_engine = OCR()  # Lazy: heavy OCR runtime loads only when needed.

        try:
            def progress(percent: int, message: str) -> None:
                if percent in {5, 15, 42, 55}:
                    print(f"    {percent:3d}% {message}")

            ocr_data = ocr_engine.recognize_pdf(source, progress_callback=progress)
            cache_key = cache.save_ocr(source, ocr_data, cache_key=cache_key)
            if parser_engine is not None:
                parsed = parser_engine.parse(ocr_data, file_name=source.name)
                cache.save_parse(
                    source,
                    parsed,
                    asdict(parser_engine.last_diagnostics),
                    parser_version=get_parser_cache_version(memory.cache_key()),
                    cache_key=cache_key,
                )
            prepared += 1
            print(f"[{inspected:03d}] req={request_id:04d} prepared")
        except (OCRError, OSError, RuntimeError) as error:
            failed += 1
            print(f"[{inspected:03d}] req={request_id:04d} FAILED: {error}")

    print("\nSummary")
    print(f"  Inspected: {inspected}")
    print(f"  Existing OCR cache: {existing}")
    print(f"  Newly prepared: {prepared}")
    print(f"  Missing PDF: {missing_pdf}")
    print(f"  Failed: {failed}")
    print(f"  Cache: {cache_dir}")
    if args.dry_run:
        print("  Dry run only; run again without --dry-run to prepare missing OCR cache.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
