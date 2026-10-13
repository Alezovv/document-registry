from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app_paths import get_app_paths
from app.document_parser import DocumentParser, PARSER_VERSION
from app.gold_dataset import GOLD_FIELDS, GoldDatasetError, evaluate_request, load_gold_dataset


BASELINE_FORMAT_VERSION = 1


def _load_cached_ocr(cache_dir: Path, sha256: str) -> dict[str, Any] | None:
    path = cache_dir / f"{sha256}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    ocr = payload.get("ocr") if isinstance(payload, dict) else None
    if not isinstance(ocr, dict) or not str(ocr.get("text", "")).strip():
        return None
    return {
        "text": str(ocr.get("text", "")),
        "blocks": list(ocr.get("blocks") or []),
        "_cache_key": sha256,
    }


def _load_baseline(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GoldDatasetError(f"Не удалось прочитать baseline: {error}") from error
    if not isinstance(payload, dict) or int(payload.get("format_version", 0) or 0) != BASELINE_FORMAT_VERSION:
        raise GoldDatasetError("Неподдерживаемый baseline format_version.")
    return payload


def _summary(evaluations: dict[str, dict[str, dict[str, bool]]]) -> dict[str, dict[str, int]]:
    stats = {field: Counter() for field in GOLD_FIELDS}
    for fields in evaluations.values():
        for field, result in fields.items():
            stats[field]["evaluated"] += 1
            stats[field]["exact"] += int(bool(result.get("exact")))
            stats[field]["compatible"] += int(bool(result.get("compatible")))
    return {field: dict(counter) for field, counter in stats.items() if counter.get("evaluated")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail a release when the parser regresses on reviewed Gold Dataset documents.")
    parser.add_argument("--gold", type=Path, default=ROOT / "gold" / "gold_dataset.json")
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=ROOT / "gold" / "baseline.json")
    parser.add_argument("--write-baseline", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    try:
        documents = load_gold_dataset(args.gold, reviewed_only=True)
    except GoldDatasetError as error:
        print(f"GOLD DATASET ERROR: {error}", file=sys.stderr)
        return 2
    if not documents:
        print("GOLD GATE SKIPPED: no reviewed documents in Gold Dataset.")
        return 0

    paths = get_app_paths()
    cache_dir = args.cache or (paths.data / "parser_cache")
    parser_engine = DocumentParser(correction_memory=None)
    evaluations: dict[str, dict[str, dict[str, bool]]] = {}
    missing_cache: list[str] = []

    for document in documents:
        ocr = _load_cached_ocr(cache_dir, document.sha256)
        if ocr is None:
            missing_cache.append(document.sha256)
            continue
        result = parser_engine.parse(ocr, file_name=document.file_name)
        evaluations[document.sha256] = evaluate_request(result, document)
        # Drop the largest object before the next document. The gate deliberately
        # processes exactly one cached OCR payload at a time.
        ocr = None
        result = None

    if missing_cache:
        print("GOLD GATE FAILED: reviewed documents without OCR cache:", file=sys.stderr)
        for digest in missing_cache:
            print(f"  {digest[:16]}…", file=sys.stderr)
        return 3

    summary = _summary(evaluations)
    baseline_target = args.write_baseline
    if baseline_target is not None:
        payload = {
            "format_version": BASELINE_FORMAT_VERSION,
            "parser_version": PARSER_VERSION,
            "documents": evaluations,
            "summary": summary,
        }
        baseline_target.parent.mkdir(parents=True, exist_ok=True)
        baseline_target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"GOLD BASELINE WRITTEN: {baseline_target}")
        return 0

    try:
        baseline = _load_baseline(args.baseline)
    except GoldDatasetError as error:
        print(f"GOLD GATE ERROR: {error}", file=sys.stderr)
        return 4

    previous_docs = baseline.get("documents") if isinstance(baseline, dict) else {}
    regressions: list[tuple[str, str, str]] = []
    improvements = 0
    for sha256, fields in evaluations.items():
        old_fields = previous_docs.get(sha256, {}) if isinstance(previous_docs, dict) else {}
        for field, current in fields.items():
            old = old_fields.get(field) if isinstance(old_fields, dict) else None
            if not isinstance(old, dict):
                regressions.append((sha256, field, "missing-baseline"))
                continue
            for metric in ("compatible", "exact"):
                old_value = bool(old.get(metric))
                new_value = bool(current.get(metric))
                if old_value and not new_value:
                    regressions.append((sha256, field, metric))
                elif not old_value and new_value:
                    improvements += 1

    report = {
        "format_version": 1,
        "parser_version": PARSER_VERSION,
        "reviewed_documents": len(documents),
        "summary": summary,
        "regression_count": len(regressions),
        "improvement_count": improvements,
        # Privacy-safe: no expected or predicted text values are written.
        "regressions": [
            {"sha256": sha, "field": field, "metric": metric}
            for sha, field, metric in regressions
        ],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Gold documents: {len(documents)}; improvements: {improvements}; regressions: {len(regressions)}")
    for field in GOLD_FIELDS:
        stats = summary.get(field)
        if not stats:
            continue
        total = int(stats.get("evaluated", 0))
        print(f"  {field:16s} exact={stats.get('exact', 0)}/{total} compatible={stats.get('compatible', 0)}/{total}")
    if regressions:
        print("GOLD GATE FAILED:", file=sys.stderr)
        for sha, field, metric in regressions[:50]:
            print(f"  {sha[:12]}… {field} {metric}", file=sys.stderr)
        return 1
    print("GOLD GATE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
