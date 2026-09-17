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


def write_self_check_report(result: SelfCheckResult, output: str | Path) -> Path:
    target = Path(output).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
