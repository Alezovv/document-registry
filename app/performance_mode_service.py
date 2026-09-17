from __future__ import annotations

from dataclasses import dataclass
import sys

from app.performance_diagnostics import PerformanceDiagnostics
from app.resource_profile import (
    ResourceProfile,
    configure_process_environment,
    detect_resource_profile,
)


@dataclass(frozen=True, slots=True)
class PerformanceApplyResult:
    profile: ResourceProfile
    model_reset: bool = False


def apply_performance_mode(
    performance_mode: str,
    *,
    current_profile: ResourceProfile | None = None,
    diagnostics: PerformanceDiagnostics | None = None,
) -> PerformanceApplyResult:
    """Apply a runtime performance mode and keep diagnostics/OCR in sync.

    OCR normally remains warm for the entire application session. An explicit
    mode change is the only exception: ONNX thread pools are immutable after
    session creation, so a loaded engine is dropped once and recreated lazily
    for the next scanned document with the new limits.
    """
    new_profile = detect_resource_profile(performance_mode)
    configure_process_environment(new_profile)

    if diagnostics is not None:
        diagnostics.set_profile(new_profile)

    model_reset = False
    if current_profile is not None:
        old_engine_key = (
            current_profile.worker_threads,
            current_profile.memory_efficient_ocr,
        )
        new_engine_key = (
            new_profile.worker_threads,
            new_profile.memory_efficient_ocr,
        )
        if old_engine_key != new_engine_key:
            ocr_module = sys.modules.get("app.ocr")
            ocr_class = getattr(ocr_module, "OCR", None) if ocr_module is not None else None
            if ocr_class is not None and getattr(ocr_class, "_shared_ocr", None) is not None:
                model_reset = bool(ocr_class.reset_shared_engine())

    return PerformanceApplyResult(new_profile, model_reset)
