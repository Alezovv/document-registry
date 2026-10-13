from __future__ import annotations

from dataclasses import dataclass
import os
import sys
import time
from typing import Final

from app.memory_metrics import process_memory_info, system_memory_status
from app.resource_profile import ResourceProfile, resource_profile_from_environment

_MIB: Final = 1024 ** 2
_GIB: Final = 1024 ** 3


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    profile_name: str
    performance_mode: str
    logical_cpus: int
    total_ram_bytes: int
    available_ram_bytes: int
    process_ram_bytes: int
    process_peak_ram_bytes: int
    memory_budget_bytes: int
    memory_pressure: str
    process_cpu_percent: float
    ocr_threads: int
    ocr_dpi: int
    ocr_retry_dpi: int
    ocr_model_loaded: bool
    memory_mode: str


class PerformanceDiagnostics:
    """Lightweight read-only runtime diagnostics without psutil/polling threads."""

    def __init__(self, profile: ResourceProfile | None = None) -> None:
        self.profile = profile or resource_profile_from_environment()
        self._last_wall = time.monotonic()
        self._last_cpu = time.process_time()
        self._observed_peak = 0

    def set_profile(self, profile: ResourceProfile) -> None:
        self.profile = profile
        self._last_wall = time.monotonic()
        self._last_cpu = time.process_time()

    def snapshot(self) -> PerformanceSnapshot:
        now_wall = time.monotonic()
        now_cpu = time.process_time()
        elapsed = max(0.000001, now_wall - self._last_wall)
        cpu_elapsed = max(0.0, now_cpu - self._last_cpu)
        self._last_wall = now_wall
        self._last_cpu = now_cpu

        logical_cpus = max(1, self.profile.logical_cpus or (os.cpu_count() or 1))
        process_cpu = min(100.0, max(0.0, cpu_elapsed / elapsed / logical_cpus * 100.0))
        total_ram, available_ram = system_memory_status()
        proc = process_memory_info()
        self._observed_peak = max(self._observed_peak, proc.current_bytes, proc.peak_bytes)
        budget = 1024 * _MIB if self.profile.economy_memory_policy else 0
        pressure = _memory_pressure_label(proc.current_bytes, self.profile.economy_memory_policy)

        return PerformanceSnapshot(
            profile_name=self.profile.name,
            performance_mode=self.profile.performance_mode_label,
            logical_cpus=logical_cpus,
            total_ram_bytes=total_ram or int(max(0.0, self.profile.total_ram_gb) * _GIB),
            available_ram_bytes=available_ram,
            process_ram_bytes=proc.current_bytes,
            process_peak_ram_bytes=self._observed_peak,
            memory_budget_bytes=budget,
            memory_pressure=pressure,
            process_cpu_percent=process_cpu,
            ocr_threads=self.profile.worker_threads,
            ocr_dpi=self.profile.ocr_dpi,
            ocr_retry_dpi=self.profile.retry_dpi,
            ocr_model_loaded=_ocr_model_loaded(),
            memory_mode=(
                "Экономный ≤1 ГБ" if self.profile.economy_memory_policy
                else ("Оптимизированный" if self.profile.memory_efficient_ocr else "Быстрый кэш")
            ),
        )


def _memory_pressure_label(current_bytes: int, economy: bool) -> str:
    if not economy:
        return "Обычный"
    mib = max(0, int(current_bytes)) / _MIB
    if mib >= 1120:
        return "Критический"
    if mib >= 1024:
        return "Пауза OCR"
    if mib >= 930:
        return "Выгрузка OCR"
    if mib >= 850:
        return "Очистка"
    return "Норма"


def format_memory(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= _GIB:
        return f"{value / _GIB:.1f} ГБ"
    return f"{value / _MIB:.0f} МБ"


def _ocr_model_loaded() -> bool:
    module = sys.modules.get("app.ocr")
    if module is None:
        return False
    ocr_class = getattr(module, "OCR", None)
    return bool(ocr_class is not None and getattr(ocr_class, "_shared_ocr", None) is not None)
