from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib
import json
from pathlib import Path
import tempfile
from typing import Iterable

from app.app_paths import AppPaths, ensure_runtime_directories, get_app_paths


@dataclass(slots=True)
class CheckItem:
    name: str
    ok: bool
    details: str


@dataclass(slots=True)
class SelfCheckResult:
    ok: bool
    items: list[CheckItem]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "items": [asdict(item) for item in self.items],
        }


REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("PySide6", "GUI"),
    ("numpy", "NumPy"),
    ("pymupdf", "PyMuPDF"),
    ("onnxruntime", "ONNX Runtime"),
    ("paddleocr", "PaddleOCR"),
    ("openpyxl", "Excel export"),
)


def _check_imports(imports: Iterable[tuple[str, str]]) -> list[CheckItem]:
    items: list[CheckItem] = []
    for module_name, label in imports:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "")
            details = f"{label} доступен" + (f" ({version})" if version else "")
            items.append(CheckItem(f"import:{module_name}", True, details))
        except Exception as error:
            items.append(CheckItem(f"import:{module_name}", False, f"{label}: {error}"))
    return items


def perform_self_check(
    paths: AppPaths | None = None,
    *,
    check_imports: bool = True,
) -> SelfCheckResult:
    """Validate a frozen/portable installation without starting the GUI or OCR model.

    The check is intentionally read/lightweight: it imports packaged dependencies
    and verifies local model files, but it never initializes PaddleOCR inference.
    """
    paths = ensure_runtime_directories(paths or get_app_paths())
    items: list[CheckItem] = []

    try:
        with tempfile.NamedTemporaryFile(
            dir=paths.runtime,
            prefix=".registry_selfcheck_",
            delete=True,
        ):
            pass
        items.append(CheckItem("runtime:writable", True, str(paths.runtime)))
    except OSError as error:
        items.append(CheckItem("runtime:writable", False, f"{paths.runtime}: {error}"))

    icon = paths.assets / "app_icon.png"
    items.append(CheckItem("asset:icon", icon.is_file(), str(icon)))

    models_root = paths.models / "paddle" / "official_models"
    required_models = (
        models_root / "PP-OCRv5_mobile_det_onnx" / "inference.onnx",
        models_root / "eslav_PP-OCRv5_mobile_rec_onnx" / "inference.onnx",
    )
    for model_file in required_models:
        items.append(
            CheckItem(
                f"model:{model_file.parent.name}",
                model_file.is_file(),
                str(model_file),
            )
        )

    if check_imports:
        items.extend(_check_imports(REQUIRED_IMPORTS))

    return SelfCheckResult(ok=all(item.ok for item in items), items=items)



def perform_ocr_runtime_self_check(paths: AppPaths | None = None) -> SelfCheckResult:
    """Initialize the real OCR engine without processing a document.

    This is intentionally separate from the lightweight self-check because it
    loads PaddleOCR/ONNX models. The Windows build invokes it once after copying
    external models, catching missing PyInstaller PaddleX pipeline resources
    before a release ZIP is created.
    """
    paths = ensure_runtime_directories(paths or get_app_paths())
    items: list[CheckItem] = []
    try:
        from app.ocr import OCR

        engine = OCR()._get_engine()
        items.append(
            CheckItem(
                "ocr:runtime",
                engine is not None,
                "PaddleOCR pipeline initialized",
            )
        )
    except Exception as error:
        items.append(CheckItem("ocr:runtime", False, str(error)))
    return SelfCheckResult(ok=all(item.ok for item in items), items=items)



def perform_ner_runtime_self_check(paths: AppPaths | None = None) -> SelfCheckResult:
    """Force-load the optional ONNX NER model when it is installed.

    A missing model is not an application failure because heuristic NER is a
    supported fallback. When model files are present, however, this check runs
    a real ONNX inference so frozen-build dependency problems are caught before
    publishing.
    """
    paths = ensure_runtime_directories(paths or get_app_paths())
    items: list[CheckItem] = []
    try:
        from app.ner_onnx import OnnxRussianNerEngine
        from app.ner_runtime import default_ner_model_dir

        model_dir = default_ner_model_dir(paths.models)
        if not OnnxRussianNerEngine.required_files_present(model_dir):
            items.append(CheckItem(
                "ner:runtime",
                True,
                f"ML NER model is not installed; heuristic fallback will be used ({model_dir})",
            ))
            return SelfCheckResult(ok=True, items=items)

        engine = OnnxRussianNerEngine(model_dir)
        sample = "Иванов Иван Иванович находится в Москве."
        entities = engine.extract(sample, [{
            "text": sample,
            "x1": 10, "y1": 10, "x2": 900, "y2": 40,
            "page_width": 1000, "page_height": 1400,
            "confidence": 0.99, "page_index": 0,
        }])
        metrics = engine.last_metrics
        items.append(CheckItem(
            "ner:runtime",
            True,
            "ONNX NER initialized and inference completed "
            f"({metrics.get('model', '')}, {metrics.get('inference_ms', 0)} ms, entities={len(entities)})",
        ))
    except Exception as error:
        items.append(CheckItem("ner:runtime", False, str(error)))
    return SelfCheckResult(ok=all(item.ok for item in items), items=items)

def write_self_check_report(result: SelfCheckResult, output: str | Path) -> Path:
    target = Path(output).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
