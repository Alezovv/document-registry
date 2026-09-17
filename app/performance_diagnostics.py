from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
import platform
import sys
import time
from typing import Final

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
    process_cpu_percent: float
    ocr_threads: int
    ocr_dpi: int
    ocr_retry_dpi: int
    ocr_model_loaded: bool
    memory_mode: str


class PerformanceDiagnostics:
    """Lightweight read-only runtime diagnostics.

    The class deliberately avoids psutil and any polling threads. A snapshot is
    collected only when the UI asks for it.
    """

    def __init__(self, profile: ResourceProfile | None = None) -> None:
        self.profile = profile or resource_profile_from_environment()
        self._last_wall = time.monotonic()
        self._last_cpu = time.process_time()

    def set_profile(self, profile: ResourceProfile) -> None:
        """Replace the active runtime profile without recreating the diagnostics UI."""
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
        # Percentage of total machine CPU capacity, not a single-core scale.
        process_cpu = min(100.0, max(0.0, cpu_elapsed / elapsed / logical_cpus * 100.0))
        total_ram, available_ram = _memory_status()

        return PerformanceSnapshot(
            profile_name=self.profile.name,
            performance_mode=self.profile.performance_mode_label,
            logical_cpus=logical_cpus,
            total_ram_bytes=total_ram,
            available_ram_bytes=available_ram,
            process_ram_bytes=_process_working_set_bytes(),
            process_cpu_percent=process_cpu,
            ocr_threads=self.profile.worker_threads,
            ocr_dpi=self.profile.ocr_dpi,
            ocr_retry_dpi=self.profile.retry_dpi,
            ocr_model_loaded=_ocr_model_loaded(),
            memory_mode=(
                "Оптимизированный"
                if self.profile.memory_efficient_ocr
                else "Быстрый кэш"
            ),
        )


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


def _memory_status() -> tuple[int, int]:
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
                return int(state.ullTotalPhys), int(state.ullAvailPhys)

        if hasattr(os, "sysconf"):
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            return total_pages * page_size, available_pages * page_size
    except (AttributeError, OSError, ValueError):
        pass

    # Fall back to the already detected total memory. Available RAM may be unknown.
    profile = resource_profile_from_environment()
    return int(max(0.0, profile.total_ram_gb) * _GIB), 0


def _windows_process_working_set_bytes() -> int:
    """Return the current process working set on Windows.

    Explicit ctypes signatures matter here: without a HANDLE restype on 64-bit
    Windows the pseudo handle may be truncated and GetProcessMemoryInfo can
    silently fail, which used to make the UI show 0 MB.
    """
    try:
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel32.GetCurrentProcess()

        get_memory_info = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if get_memory_info is None:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            get_memory_info = psapi.GetProcessMemoryInfo

        get_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        get_memory_info.restype = wintypes.BOOL

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if get_memory_info(handle, ctypes.byref(counters), counters.cb):
            working_set = int(counters.WorkingSetSize)
            if working_set > 0:
                return working_set
            # PrivateUsage is not identical to the working set, but is a useful
            # fallback if a particular Windows build reports a zero WS value.
            return int(counters.PrivateUsage)
    except (AttributeError, OSError, ValueError):
        pass
    return 0


def _process_working_set_bytes() -> int:
    try:
        if platform.system() == "Windows":
            return _windows_process_working_set_bytes()

        proc_statm = "/proc/self/statm"
        if os.path.isfile(proc_statm):
            with open(proc_statm, "r", encoding="ascii") as stream:
                fields = stream.read().split()
            if len(fields) >= 2:
                page_size = int(os.sysconf("SC_PAGE_SIZE"))
                return int(fields[1]) * page_size

        import resource

        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if platform.system() == "Darwin":
            return rss
        return rss * 1024
    except (AttributeError, OSError, ValueError, ImportError):
        return 0
