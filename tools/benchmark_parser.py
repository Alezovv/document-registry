from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
from statistics import mean
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app_paths import get_app_paths
from app.document_parser import DocumentParser
from app.parser_correction_memory import ParserCorrectionMemory


def load_ocr_samples(cache_dir: Path, limit: int) -> list[tuple[str, dict]]:
    samples: list[tuple[str, dict]] = []
    entries = sorted(cache_dir.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in entries:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        ocr = payload.get("ocr") if isinstance(payload, dict) else None
        if not isinstance(ocr, dict) or not str(ocr.get("text", "")).strip():
            continue
        samples.append((path.stem[:12], ocr))
        if len(samples) >= limit:
            break
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate parser decisions on existing OCR cache without rerunning OCR."
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum cached documents (default: 50)")
    parser.add_argument("--cache", type=Path, default=None, help="Parser cache directory")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional privacy-safe JSON report path")
    args = parser.parse_args()

    paths = get_app_paths()
    cache_dir = args.cache or (paths.data / "parser_cache")
    samples = load_ocr_samples(cache_dir, max(1, args.limit))
    if not samples:
        print(f"No OCR cache samples found in: {cache_dir}", file=sys.stderr)
        return 2

    correction_memory = ParserCorrectionMemory.for_data_dir(paths.data)
    document_parser = DocumentParser(correction_memory=correction_memory)
    rows: list[dict] = []
    decision_counter: Counter[str] = Counter()
    review_counter: Counter[str] = Counter()
    trusted_label_counter: Counter[str] = Counter()
    agreement_values: list[float] = []
    hybrid_change_counter: Counter[str] = Counter()

    print(f"Parser calibration: {len(samples)} cached document(s)")
    print("OCR is NOT rerun. Report does not contain OCR text or extracted field values.\n")

    for index, (key, ocr) in enumerate(samples, 1):
        started = perf_counter()
        document_parser.parse(ocr)
        elapsed_ms = (perf_counter() - started) * 1000
        d = document_parser.last_diagnostics
        decisions = dict(d.field_decisions)
        decision_counter.update(decisions.values())
        review_counter.update(d.review_fields)
        trusted_label_counter.update(d.ner_trusted_entity_counts)
        hybrid_change_counter.update(d.hybrid_changed_fields)
        agreement = d.ner_comparison.get("compatible_agreement_ratio") if isinstance(d.ner_comparison, dict) else None
        if isinstance(agreement, (int, float)):
            agreement_values.append(float(agreement))
        rows.append({
            "key": key,
            "elapsed_ms": round(elapsed_ms, 3),
            "document_type": d.document_type,
            "document_type_confidence": d.document_type_confidence,
            "ner_engine": d.ner_engine,
            "ner_compatible_agreement_ratio": agreement,
            "trusted_ml_entity_counts": dict(d.ner_trusted_entity_counts),
            "field_decisions": decisions,
            "review_fields": list(d.review_fields),
            "field_confidence": dict(d.field_confidence),
            "hybrid_changed_fields": list(d.hybrid_changed_fields),
            "hybrid_candidate_counts": dict(d.hybrid_candidate_counts),
        })
        review_text = ",".join(d.review_fields) if d.review_fields else "-"
        agreement_text = f"{float(agreement):.2f}" if isinstance(agreement, (int, float)) else "-"
        print(
            f"[{index:02d}/{len(samples):02d}] {key}  "
            f"{elapsed_ms:6.1f}ms  type={d.document_type:<10}  "
            f"NER-compat={agreement_text}  trusted={sum(d.ner_trusted_entity_counts.values()):2d}  "
            f"hybrid={len(d.hybrid_changed_fields):1d}  review={review_text}"
        )

    summary = {
        "documents": len(rows),
        "mean_parse_ms": round(mean(row["elapsed_ms"] for row in rows), 3),
        "mean_ner_agreement_ratio": round(mean(agreement_values), 3) if agreement_values else None,
        "decision_counts": dict(decision_counter),
        "review_field_counts": dict(review_counter),
        "trusted_ml_label_counts": dict(trusted_label_counter),
        "hybrid_changed_field_counts": dict(hybrid_change_counter),
    }
    print("\nSummary")
    print(f"  Mean parse time: {summary['mean_parse_ms']:.1f} ms/document")
    if summary["mean_ner_agreement_ratio"] is not None:
        print(f"  Mean compatible ML/heuristic NER agreement: {summary['mean_ner_agreement_ratio']:.3f}")
    print(f"  Decisions: {summary['decision_counts']}")
    print(f"  Review fields: {summary['review_field_counts']}")
    print(f"  Trusted ML entities by label: {summary['trusted_ml_label_counts']}")
    print(f"  Hybrid changed fields: {summary['hybrid_changed_field_counts']}")
    print("  Note: review counts are calibration signals, not accuracy scores.")

    if args.json_out:
        report = {
            "format_version": 1,
            "cache": str(cache_dir),
            "summary": summary,
            "documents": rows,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  JSON report: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
