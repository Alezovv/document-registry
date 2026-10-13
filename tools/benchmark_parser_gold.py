from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import re
import sqlite3
from statistics import mean
import sys
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app_paths import get_app_paths
from app.document_parser import DocumentParser
from app.ner_runtime import get_default_ner_engine
from app.parser_cache import ParserCache
from app.parser_core import FieldNormalizer
from app.gold_dataset import values_compatible
from app.parser_correction_memory import ParserCorrectionMemory
from app.portable_paths import decode_runtime_path

FIELDS = (
    "received_date",
    "from_department",
    "from_position",
    "from_person",
    "to_person",
    "request_subject",
)


def _read_cache_entry(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    ocr = payload.get("ocr") if isinstance(payload, dict) else None
    if not isinstance(ocr, dict) or not str(ocr.get("text", "")).strip():
        return None
    return ocr


def _load_requests(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, received_date, from_department, from_position,
                   from_person, to_person, request_subject, source_document
            FROM requests
            WHERE source_document IS NOT NULL AND source_document != ''
            ORDER BY id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _safe_rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare production hybrid parser and deterministic baseline against "
            "the values currently saved in requests.db, using existing OCR cache only."
        )
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum matched documents (default: 50)")
    parser.add_argument("--cache", type=Path, default=None, help="Parser cache directory")
    parser.add_argument("--database", type=Path, default=None, help="SQLite database path")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional privacy-safe JSON report")
    args = parser.parse_args()

    paths = get_app_paths()
    cache_dir = args.cache or (paths.data / "parser_cache")
    db_path = args.database or paths.database
    rows = _load_requests(db_path)
    if not rows:
        print(f"No requests with documents found in: {db_path}", file=sys.stderr)
        return 2

    memory = ParserCorrectionMemory.for_data_dir(paths.data)
    engine = get_default_ner_engine()
    production = DocumentParser(ner_engine=engine, correction_memory=memory, hybrid_enabled=True)
    baseline = DocumentParser(ner_engine=engine, correction_memory=memory, hybrid_enabled=False)

    field_stats: dict[str, Counter[str]] = {field: Counter() for field in FIELDS}
    hybrid_effects: dict[str, Counter[str]] = {field: Counter() for field in FIELDS}
    documents: list[dict[str, Any]] = []
    elapsed: list[float] = []
    skipped_missing_pdf = 0
    skipped_missing_cache = 0

    print("Gold/proxy benchmark against values already saved in requests.db")
    print("OCR is NOT rerun. No field values or OCR text are printed or written to the report.")
    print("Important: saved DB values are only a useful gold proxy if an operator has reviewed them.\n")

    for row in rows:
        if len(documents) >= max(1, args.limit):
            break
        source = decode_runtime_path(row.get("source_document"), paths.runtime)
        if source is None or not source.is_file():
            skipped_missing_pdf += 1
            continue
        try:
            cache_key = ParserCache.content_hash(source)
        except OSError:
            skipped_missing_pdf += 1
            continue
        ocr = _read_cache_entry(cache_dir / f"{cache_key}.json")
        if ocr is None:
            skipped_missing_cache += 1
            continue

        started = perf_counter()
        baseline_result = baseline.parse(ocr, file_name=source.name)
        production_result = production.parse(ocr, file_name=source.name)
        elapsed_ms = (perf_counter() - started) * 1000.0
        elapsed.append(elapsed_ms)

        mismatches: list[str] = []
        changed_fields: list[str] = []
        improved: list[str] = []
        harmed: list[str] = []
        evaluated = 0
        exact_hits = 0
        compatible_hits = 0

        per_field: dict[str, dict[str, Any]] = {}
        for field in FIELDS:
            gold = str(row.get(field) or "").strip()
            if not gold:
                continue  # blank saved value is not treated as a negative label
            evaluated += 1
            prod = str(getattr(production_result, field, "") or "").strip()
            base = str(getattr(baseline_result, field, "") or "").strip()

            gold_key = FieldNormalizer.comparison_key(field, gold)
            prod_key = FieldNormalizer.comparison_key(field, prod)
            base_key = FieldNormalizer.comparison_key(field, base)
            prod_exact = bool(gold_key) and prod_key == gold_key
            base_exact = bool(gold_key) and base_key == gold_key
            prod_compatible = prod_exact or values_compatible(field, prod, gold)
            base_compatible = base_exact or values_compatible(field, base, gold)
            changed = prod_key != base_key

            stats = field_stats[field]
            stats["evaluated"] += 1
            stats["production_exact"] += int(prod_exact)
            stats["production_compatible"] += int(prod_compatible)
            stats["baseline_exact"] += int(base_exact)
            stats["baseline_compatible"] += int(base_compatible)

            effect = hybrid_effects[field]
            if changed:
                effect["changed"] += 1
                changed_fields.append(field)
                if prod_compatible and not base_compatible:
                    effect["improved"] += 1
                    improved.append(field)
                elif base_compatible and not prod_compatible:
                    effect["harmed"] += 1
                    harmed.append(field)
                else:
                    effect["neutral_or_unknown"] += 1

            exact_hits += int(prod_exact)
            compatible_hits += int(prod_compatible)
            if not prod_compatible:
                mismatches.append(field)

            per_field[field] = {
                "production_exact": prod_exact,
                "production_compatible": prod_compatible,
                "baseline_exact": base_exact,
                "baseline_compatible": base_compatible,
                "hybrid_changed": changed,
            }

        item = {
            "request_id": int(row["id"]),
            "cache_key": cache_key[:12],
            "elapsed_ms": round(elapsed_ms, 3),
            "evaluated_fields": evaluated,
            "production_exact_hits": exact_hits,
            "production_compatible_hits": compatible_hits,
            "mismatch_fields": mismatches,
            "hybrid_changed_fields": changed_fields,
            "hybrid_improved_fields": improved,
            "hybrid_harmed_fields": harmed,
            "fields": per_field,
        }
        documents.append(item)
        mismatch_text = ",".join(mismatches) if mismatches else "-"
        change_text = ",".join(changed_fields) if changed_fields else "-"
        print(
            f"[{len(documents):02d}] req={int(row['id']):04d} {cache_key[:12]}  "
            f"fields={evaluated:1d} compat={compatible_hits}/{evaluated}  "
            f"hybrid_changed={change_text}  mismatch={mismatch_text}"
        )

    if not documents:
        print("No request could be matched to both an existing PDF and OCR cache.", file=sys.stderr)
        return 3

    summary_fields: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        stats = field_stats[field]
        effect = hybrid_effects[field]
        total = int(stats["evaluated"])
        summary_fields[field] = {
            "evaluated": total,
            "production_exact": int(stats["production_exact"]),
            "production_exact_rate": _safe_rate(int(stats["production_exact"]), total),
            "production_compatible": int(stats["production_compatible"]),
            "production_compatible_rate": _safe_rate(int(stats["production_compatible"]), total),
            "baseline_exact": int(stats["baseline_exact"]),
            "baseline_exact_rate": _safe_rate(int(stats["baseline_exact"]), total),
            "baseline_compatible": int(stats["baseline_compatible"]),
            "baseline_compatible_rate": _safe_rate(int(stats["baseline_compatible"]), total),
            "hybrid_changed": int(effect["changed"]),
            "hybrid_improved": int(effect["improved"]),
            "hybrid_harmed": int(effect["harmed"]),
            "hybrid_neutral_or_unknown": int(effect["neutral_or_unknown"]),
        }

    total_evaluated = sum(item["evaluated_fields"] for item in documents)
    total_prod_compatible = sum(item["production_compatible_hits"] for item in documents)
    total_improved = sum(v["hybrid_improved"] for v in summary_fields.values())
    total_harmed = sum(v["hybrid_harmed"] for v in summary_fields.values())
    summary = {
        "documents": len(documents),
        "evaluated_fields": total_evaluated,
        "production_compatible": total_prod_compatible,
        "production_compatible_rate": _safe_rate(total_prod_compatible, total_evaluated),
        "hybrid_improved": total_improved,
        "hybrid_harmed": total_harmed,
        "mean_ab_parse_ms": round(mean(elapsed), 3) if elapsed else 0.0,
        "skipped_missing_pdf": skipped_missing_pdf,
        "skipped_missing_cache": skipped_missing_cache,
        "fields": summary_fields,
    }

    print("\nSummary (saved DB values as proxy gold)")
    print(
        f"  Production compatible: {total_prod_compatible}/{total_evaluated} "
        f"({(summary['production_compatible_rate'] or 0.0) * 100:.1f}%)"
    )
    print(f"  Hybrid effects vs deterministic baseline: improved={total_improved}, harmed={total_harmed}")
    print(f"  Mean A/B parse time: {summary['mean_ab_parse_ms']:.1f} ms/document")
    print("  Per field:")
    for field in FIELDS:
        item = summary_fields[field]
        total = item["evaluated"]
        if not total:
            continue
        prod_rate = (item["production_compatible_rate"] or 0.0) * 100
        base_rate = (item["baseline_compatible_rate"] or 0.0) * 100
        print(
            f"    {field:<16} prod={item['production_compatible']:>3}/{total:<3} {prod_rate:5.1f}%  "
            f"base={item['baseline_compatible']:>3}/{total:<3} {base_rate:5.1f}%  "
            f"changed={item['hybrid_changed']:>2} +{item['hybrid_improved']}/-{item['hybrid_harmed']}"
        )
    if skipped_missing_pdf or skipped_missing_cache:
        print(f"  Skipped: missing PDF={skipped_missing_pdf}, missing OCR cache={skipped_missing_cache}")
    if skipped_missing_cache:
        suggested = max(args.limit, min(150, args.limit + skipped_missing_cache))
        print(
            "  Coverage hint: prepare missing cache with "
            f"python tools\\prepare_gold_cache.py --limit {suggested}"
        )

    if args.json_out:
        report = {
            "format_version": 1,
            "database": str(db_path),
            "cache": str(cache_dir),
            "privacy": "No OCR text or field values are included.",
            "summary": summary,
            "documents": documents,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  JSON report: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
