from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
import os
import platform
from pathlib import Path

_GIB = 1024 ** 3
VALID_PERFORMANCE_MODES = {"economy", "auto", "performance"}
PERFORMANCE_MODE_LABELS = {
    "economy": "Экономный",
    "auto": "Авто",
    "performance": "Производительный",
}


def normalize_performance_mode(value: str | None) -> str:
    value = str(value or "auto").strip().lower()
    return value if value in VALID_PERFORMANCE_MODES else "auto"


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    name: str
    logical_cpus: int
    total_ram_gb: float
    worker_threads: int
    ocr_dpi: int
    retry_dpi: int
    max_image_width: int
    max_image_height: int
    performance_mode: str = "auto"
    _memory_efficient_override: bool | None = None

    @property
    def performance_mode_label(self) -> str:
        return PERFORMANCE_MODE_LABELS.get(self.performance_mode, "Авто")

    @property
    def memory_efficient_ocr(self) -> bool:
        """Whether ONNX should avoid retaining large transient memory arenas.

        A nominal 16 GB machine often reports 15.x GiB to Windows, so the
        threshold intentionally sits well below 16. The OCR model itself always
        stays warm; only allocator caches are affected.
        """
        if self._memory_efficient_override is not None:
            return self._memory_efficient_override
        return self.total_ram_gb <= 0 or self.total_ram_gb < 12

    @property
    def summary(self) -> str:
        memory_mode = "оптимизированная память" if self.memory_efficient_ocr else "быстрый кэш RAM"
        return (
            f"{self.performance_mode_label} · {self.name}: CPU {self.logical_cpus} потоков, "
            f"RAM {self.total_ram_gb:.1f} ГБ, OCR {self.worker_threads} поток(а), "
            f"{self.ocr_dpi}/{self.retry_dpi} DPI · {memory_mode}"
        )


def detect_total_memory_bytes() -> int:
    """Return total physical memory without requiring psutil."""
    try:
        if platform.system() == "Windows":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            state = MEMORYSTATUSEX()
            state.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
                return int(state.ullTotalPhys)
        elif hasattr(os, "sysconf"):
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return 0


def _nominal_ram_gb(actual_gib: float) -> float:
    """Map OS-reported RAM to common nominal capacities for profile thresholds."""
    if actual_gib <= 0:
        return 0.0
    for tier in (4, 8, 16, 32, 64, 128):
        if tier * 0.90 <= actual_gib <= tier * 1.10:
            return float(tier)
    return actual_gib


def _auto_profile(logical_cpus: int, ram_gb: float) -> tuple[str, int, int, int, int, int]:
    nominal = _nominal_ram_gb(ram_gb)
    if logical_cpus <= 2 or (nominal and nominal < 4):
        return "Экономный", 1, 130, 170, 1800, 2600
    if logical_cpus <= 4 or (nominal and nominal < 8):
        return "Сбалансированный", 1, 145, 185, 2100, 3000
    if logical_cpus <= 8 or (nominal and nominal < 12):
        return "Производительный", 2, 155, 200, 2400, 3400
    # 16 GB laptops deliberately keep two OCR threads in Auto to preserve
    # responsiveness for browsers/office apps. Performance mode can raise it.
    if nominal and nominal < 24:
        return "Производительный", 2, 155, 200, 2400, 3400
    if logical_cpus <= 16 or (nominal and nominal < 32):
        return "Ускоренный", 3, 165, 210, 2800, 3900
    return "Максимальный", 4, 175, 220, 3200, 4400


def choose_profile(
    logical_cpus: int,
    total_ram_bytes: int,
    performance_mode: str = "auto",
) -> ResourceProfile:
    logical_cpus = max(1, int(logical_cpus or 1))
    ram_gb = max(0.0, float(total_ram_bytes or 0) / _GIB)
    mode = normalize_performance_mode(performance_mode)

    name, threads, dpi, retry_dpi, max_width, max_height = _auto_profile(logical_cpus, ram_gb)
    nominal = _nominal_ram_gb(ram_gb)

    if mode == "economy":
        threads = 1
        memory_override = True
    elif mode == "performance":
        # Use more concurrency but always leave headroom for the OS and other apps.
        threads = min(4, logical_cpus - 1 if logical_cpus > 1 else 1, max(2, threads + 1))
        memory_override = False if nominal >= 8 else True
    else:
        memory_override = None

    return ResourceProfile(
        name,
        logical_cpus,
        ram_gb,
        max(1, threads),
        dpi,
        retry_dpi,
        max_width,
        max_height,
        mode,
        memory_override,
    )


def detect_resource_profile(performance_mode: str = "auto") -> ResourceProfile:
    return choose_profile(
        os.cpu_count() or 1,
        detect_total_memory_bytes(),
        performance_mode,
    )


def load_performance_mode(settings_file: str | Path) -> str:
    """Read only the lightweight performance-mode setting before heavy imports."""
    try:
        payload = json.loads(Path(settings_file).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return normalize_performance_mode(payload.get("performance_mode"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "auto"


def configure_process_environment(profile: ResourceProfile) -> None:
    threads = str(max(1, profile.worker_threads))
    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[variable] = threads

    os.environ["DOCUMENT_REGISTRY_PERFORMANCE_MODE"] = profile.performance_mode
    os.environ["DOCUMENT_REGISTRY_RESOURCE_PROFILE"] = profile.name
    os.environ["DOCUMENT_REGISTRY_OCR_THREADS"] = threads
    os.environ["DOCUMENT_REGISTRY_OCR_DPI"] = str(profile.ocr_dpi)
    os.environ["DOCUMENT_REGISTRY_OCR_RETRY_DPI"] = str(profile.retry_dpi)
    os.environ["DOCUMENT_REGISTRY_OCR_MAX_WIDTH"] = str(profile.max_image_width)
    os.environ["DOCUMENT_REGISTRY_OCR_MAX_HEIGHT"] = str(profile.max_image_height)
    os.environ["DOCUMENT_REGISTRY_OCR_MEMORY_EFFICIENT"] = "1" if profile.memory_efficient_ocr else "0"
    os.environ.setdefault("ORT_LOGGING_LEVEL", "3")


def resource_profile_from_environment() -> ResourceProfile:
    mode = normalize_performance_mode(os.environ.get("DOCUMENT_REGISTRY_PERFORMANCE_MODE", "auto"))
    detected = detect_resource_profile(mode)

    def env_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return default

    memory_env = os.environ.get("DOCUMENT_REGISTRY_OCR_MEMORY_EFFICIENT")
    memory_override = None if memory_env is None else memory_env == "1"
    return ResourceProfile(
        os.environ.get("DOCUMENT_REGISTRY_RESOURCE_PROFILE", detected.name),
        detected.logical_cpus,
        detected.total_ram_gb,
        env_int("DOCUMENT_REGISTRY_OCR_THREADS", detected.worker_threads),
        env_int("DOCUMENT_REGISTRY_OCR_DPI", detected.ocr_dpi),
        env_int("DOCUMENT_REGISTRY_OCR_RETRY_DPI", detected.retry_dpi),
        env_int("DOCUMENT_REGISTRY_OCR_MAX_WIDTH", detected.max_image_width),
        env_int("DOCUMENT_REGISTRY_OCR_MAX_HEIGHT", detected.max_image_height),
        mode,
        memory_override,
    )
