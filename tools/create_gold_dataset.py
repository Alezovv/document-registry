from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app_paths import get_app_paths
from app.gold_dataset import GOLD_FIELDS, GOLD_FORMAT_VERSION
from app.portable_paths import decode_runtime_path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_existing(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    docs = payload.get("documents", []) if isinstance(payload, dict) else []
    return {
        str(item.get("sha256", "")).lower(): item
        for item in docs
        if isinstance(item, dict) and item.get("sha256")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create/update a privacy-safe Gold Dataset draft from requests.db.")
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--runtime", type=Path, default=None, help="Runtime root used to resolve portable path tokens")
    parser.add_argument("--out", type=Path, default=ROOT / "gold" / "gold_dataset.json")
    parser.add_argument("--limit", type=int, default=0, help="0 = all requests with a document")
    args = parser.parse_args()

    paths = get_app_paths()
    db_path = args.database or paths.database
    runtime = (args.runtime or paths.runtime).resolve(strict=False)
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    existing = _load_existing(args.out)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        hash_expr = "source_sha256" if "source_sha256" in columns else "'' AS source_sha256"
        rows = conn.execute(
            f"""
            SELECT id, source_document, {hash_expr},
                   received_date, from_department, from_position,
                   from_person, to_person, request_subject
            FROM requests
            WHERE TRIM(COALESCE(source_document, '')) != ''
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    documents: list[dict] = []
    missing = 0
    for row in rows:
        if args.limit > 0 and len(documents) >= args.limit:
            break
        source = decode_runtime_path(row["source_document"], runtime)
        if source is None or not source.is_file():
            missing += 1
            continue
        sha256 = str(row["source_sha256"] or "").strip().lower()
        if len(sha256) != 64:
            try:
                sha256 = _hash_file(source)
            except OSError:
                missing += 1
                continue
        previous = existing.get(sha256, {})
        if bool(previous.get("reviewed")):
            # Never overwrite a human-reviewed label with a current DB value.
            expected = {
                field: str((previous.get("expected") or {}).get(field, "") or "")
                for field in GOLD_FIELDS
            }
            reviewed = True
        else:
            expected = {field: str(row[field] or "").strip() for field in GOLD_FIELDS}
            reviewed = False
        documents.append({
            "sha256": sha256,
            "file_name": source.name,
            "request_id": int(row["id"]),
            "reviewed": reviewed,
            "expected": expected,
        })

    payload = {
        "format_version": GOLD_FORMAT_VERSION,
        "documents": documents,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reviewed_count = sum(1 for item in documents if item["reviewed"])
    print(f"Gold draft: {args.out}")
    print(f"Documents: {len(documents)}; reviewed preserved: {reviewed_count}; missing PDFs: {missing}")
    print('Review expected fields manually, then set "reviewed": true for confirmed documents.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
