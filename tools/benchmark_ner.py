from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app_paths import get_app_paths
from app.ner import HeuristicRussianNerEngine, compare_entity_sets, select_trusted_ml_entities
from app.ner_onnx import OnnxRussianNerEngine
from app.ner_runtime import default_ner_model_dir


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


def signature(entities) -> set[tuple[str, str]]:
    return {(item.label, " ".join(item.text.lower().split())) for item in entities}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare heuristic and ONNX NER on existing OCR cache without rerunning OCR."
    )
    parser.add_argument("--limit", type=int, default=30, help="Maximum cached documents (default: 30)")
    parser.add_argument("--cache", type=Path, default=None, help="Parser cache directory")
    parser.add_argument("--model", type=Path, default=None, help="NER model directory")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON report path")
    args = parser.parse_args()

    paths = get_app_paths()
    cache_dir = args.cache or (paths.data / "parser_cache")
    model_dir = args.model or default_ner_model_dir(paths.models)
    if not OnnxRussianNerEngine.required_files_present(model_dir):
        print(f"ML NER model not found: {model_dir}", file=sys.stderr)
        print("Install it first: python tools/install_ner_model.py", file=sys.stderr)
        return 2
    samples = load_ocr_samples(cache_dir, max(1, args.limit))
    if not samples:
        print(f"No OCR cache samples found in: {cache_dir}", file=sys.stderr)
        return 2

    heuristic = HeuristicRussianNerEngine()
    onnx = OnnxRussianNerEngine(model_dir)
    rows = []
    print(f"NER benchmark: {len(samples)} cached document(s)")
    print(f"Model: {model_dir}")
    print("OCR is NOT rerun.\n")

    for index, (key, ocr) in enumerate(samples, 1):
        text = str(ocr.get("text", ""))
        blocks = list(ocr.get("blocks") or [])
        start = perf_counter()
        h_entities = heuristic.extract(text, blocks)
        h_ms = (perf_counter() - start) * 1000
        m_entities = onnx.extract(text, blocks)
        metrics = dict(onnx.last_metrics)
        m_ms = float(metrics.get("inference_ms") or 0.0)
        h_sig = signature(h_entities)
        m_sig = signature(m_entities)
        overlap = len(h_sig & m_sig)
        trusted = select_trusted_ml_entities(m_entities, h_entities, allow_standalone=False)
        experimental = select_trusted_ml_entities(m_entities, h_entities, allow_standalone=True)
        comparison = compare_entity_sets(
            m_entities, h_entities,
            primary_name=onnx.name,
            baseline_name=heuristic.name,
        )
        rows.append({
            "key": key,
            "heuristic_ms": round(h_ms, 3),
            "onnx_ms": m_ms,
            "heuristic_entities": len(h_entities),
            "onnx_entities": len(m_entities),
            "trusted_ml_entities": len(trusted),
            "experimental_trusted_ml_entities": len(experimental),
            "overlap": overlap,
            "agreement_ratio": comparison.get("agreement_ratio"),
            "compatible_agreement_ratio": comparison.get("compatible_agreement_ratio"),
            "by_label": comparison.get("by_label", {}),
            "primary_only_count": comparison.get("primary_only_count", 0),
            "baseline_only_count": comparison.get("baseline_only_count", 0),
            "rss_delta_mb": metrics.get("rss_delta_mb"),
        })
        print(
            f"[{index:02d}/{len(samples):02d}] {key}  "
            f"heur={len(h_entities):2d}/{h_ms:6.1f}ms  "
            f"onnx={len(m_entities):2d}/{m_ms:6.1f}ms  "
            f"trusted={len(trusted):2d}  exp={len(experimental):2d}  overlap={overlap:2d}  "
            f"exact={float(comparison.get('agreement_ratio') or 0.0):.2f}  "
            f"compat={float(comparison.get('compatible_agreement_ratio') or 0.0):.2f}"
        )

    rss_values = [float(row["rss_delta_mb"]) for row in rows if row["rss_delta_mb"] is not None]
    agreement_values = [float(row["agreement_ratio"]) for row in rows if row.get("agreement_ratio") is not None]
    compatible_values = [float(row["compatible_agreement_ratio"]) for row in rows if row.get("compatible_agreement_ratio") is not None]
    summary = {
        "documents": len(rows),
        "heuristic_mean_ms": round(mean(row["heuristic_ms"] for row in rows), 3),
        "onnx_mean_ms": round(mean(row["onnx_ms"] for row in rows), 3),
        "onnx_load_ms": round(float(onnx.last_metrics.get("load_ms") or 0.0), 3),
        "mean_agreement_ratio": round(mean(agreement_values), 3) if agreement_values else None,
        "mean_compatible_agreement_ratio": round(mean(compatible_values), 3) if compatible_values else None,
        "mean_trusted_ml_entities": round(mean(row["trusted_ml_entities"] for row in rows), 3),
        "mean_experimental_trusted_ml_entities": round(mean(row["experimental_trusted_ml_entities"] for row in rows), 3),
        "mean_rss_delta_mb": round(mean(rss_values), 3) if rss_values else None,
    }
    print("\nSummary")
    print(f"  Heuristic mean: {summary['heuristic_mean_ms']:.1f} ms/document")
    print(f"  ONNX inference mean: {summary['onnx_mean_ms']:.1f} ms/document")
    print(f"  ONNX one-time load: {summary['onnx_load_ms']:.1f} ms")
    if summary["mean_agreement_ratio"] is not None:
        print(f"  Mean exact ML/heuristic agreement: {summary['mean_agreement_ratio']:.3f}")
    if summary["mean_compatible_agreement_ratio"] is not None:
        print(f"  Mean compatible agreement: {summary['mean_compatible_agreement_ratio']:.3f}")
    print(f"  Mean trusted ML entities (production confirmation-only): {summary['mean_trusted_ml_entities']:.1f}/document")
    print(f"  Mean trusted ML entities (old experimental policy): {summary['mean_experimental_trusted_ml_entities']:.1f}/document")
    if rss_values:
        print(f"  Mean process RSS delta during inference: {summary['mean_rss_delta_mb']:.1f} MB")
    print("  Note: agreement is a calibration signal, not an accuracy score; gold-labelled documents are needed for quality F1.")

    if args.json_out:
        report = {
            "format_version": 1,
            "model": str(model_dir),
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
