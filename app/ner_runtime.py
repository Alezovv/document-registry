from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any, Iterable

from app.app_paths import get_app_paths
from app.ner import HeuristicRussianNerEngine, NerEngine, NerEntity
from app.ner_onnx import OnnxRussianNerEngine

NER_MODEL_FOLDER = "rubert_tiny_news"
VALID_NER_MODES = {"auto", "onnx", "heuristic", "off"}
_DEFAULT_ENGINE: NerEngine | None = None
_DEFAULT_ENGINE_KEY = ""
_DEFAULT_LOCK = threading.RLock()


def normalize_ner_mode(value: str | None) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in VALID_NER_MODES else "auto"


def default_ner_model_dir(model_root: str | Path | None = None) -> Path:
    root = Path(model_root) if model_root is not None else get_app_paths().models
    return root / "ner" / NER_MODEL_FOLDER


class DisabledNerEngine(NerEngine):
    name = "disabled"
    last_metrics = {"engine": "disabled", "active_engine": "disabled", "loaded": False}

    def extract(self, text: str, blocks: Iterable[dict[str, Any]] = ()) -> list[NerEntity]:
        return []


class FallbackNerEngine(NerEngine):
    """Use ONNX when available and permanently fall back after runtime failure."""

    name = "onnx-with-heuristic-fallback"

    def __init__(self, primary: NerEngine, fallback: NerEngine | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or HeuristicRussianNerEngine()
        self._disabled_reason = ""
        self.last_metrics: dict[str, Any] = {
            "engine": self.name,
            "active_engine": getattr(primary, "name", "onnx"),
            "loaded": False,
        }

    def extract(self, text: str, blocks: Iterable[dict[str, Any]] = ()) -> list[NerEntity]:
        materialized_blocks = list(blocks)
        if not self._disabled_reason:
            try:
                output = self.primary.extract(text, materialized_blocks)
                metrics = dict(getattr(self.primary, "last_metrics", {}) or {})
                metrics.setdefault("engine", self.name)
                metrics["engine"] = self.name
                metrics["active_engine"] = getattr(self.primary, "name", "onnx")
                metrics["fallback_used"] = False
                self.last_metrics = metrics
                return output
            except Exception as error:
                self._disabled_reason = str(error) or type(error).__name__

        output = self.fallback.extract(text, materialized_blocks)
        self.last_metrics = {
            "engine": self.name,
            "active_engine": getattr(self.fallback, "name", "heuristic"),
            "loaded": False,
            "fallback_used": True,
            "fallback_reason": self._disabled_reason,
            "entities": len(output),
        }
        return output


def engine_cache_key(*, mode: str | None = None, model_root: str | Path | None = None) -> str:
    selected = normalize_ner_mode(mode or os.environ.get("DOCUMENT_REGISTRY_NER_MODE"))
    if selected == "off":
        return "ner-off"
    if selected == "heuristic":
        return "heuristic-ru-v1"
    model_dir = default_ner_model_dir(model_root)
    if OnnxRussianNerEngine.required_files_present(model_dir):
        return OnnxRussianNerEngine.name
    return "heuristic-ru-v1"



def current_engine_cache_key(*, mode: str | None = None, model_root: str | Path | None = None) -> str:
    """Return the backend that will actually populate parser cache.

    After a runtime ONNX failure the process-wide fallback is sticky, so later
    cache entries must be tagged as heuristic rather than pretending ML NER was
    used.
    """
    with _DEFAULT_LOCK:
        if isinstance(_DEFAULT_ENGINE, FallbackNerEngine) and _DEFAULT_ENGINE._disabled_reason:
            return getattr(_DEFAULT_ENGINE.fallback, "name", "heuristic-ru-v1")
    return engine_cache_key(mode=mode, model_root=model_root)

def create_default_ner_engine(
    *,
    mode: str | None = None,
    model_root: str | Path | None = None,
) -> NerEngine:
    selected = normalize_ner_mode(mode or os.environ.get("DOCUMENT_REGISTRY_NER_MODE"))
    if selected == "off":
        return DisabledNerEngine()
    if selected == "heuristic":
        return HeuristicRussianNerEngine()

    model_dir = default_ner_model_dir(model_root)
    if OnnxRussianNerEngine.required_files_present(model_dir):
        return FallbackNerEngine(OnnxRussianNerEngine(model_dir))
    if selected == "onnx":
        engine = FallbackNerEngine(OnnxRussianNerEngine(model_dir))
        engine._disabled_reason = f"NER model files are missing: {model_dir}"
        return engine
    return HeuristicRussianNerEngine()


def get_default_ner_engine(
    *,
    mode: str | None = None,
    model_root: str | Path | None = None,
) -> NerEngine:
    """Return one process-wide engine so the ONNX model is loaded only once."""
    global _DEFAULT_ENGINE, _DEFAULT_ENGINE_KEY
    cache_key = engine_cache_key(mode=mode, model_root=model_root)
    with _DEFAULT_LOCK:
        if _DEFAULT_ENGINE is None or _DEFAULT_ENGINE_KEY != cache_key:
            _DEFAULT_ENGINE = create_default_ner_engine(mode=mode, model_root=model_root)
            _DEFAULT_ENGINE_KEY = cache_key
        return _DEFAULT_ENGINE


def reset_default_ner_engine_for_tests() -> None:
    global _DEFAULT_ENGINE, _DEFAULT_ENGINE_KEY
    with _DEFAULT_LOCK:
        _DEFAULT_ENGINE = None
        _DEFAULT_ENGINE_KEY = ""
