from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
import platform


@dataclass(frozen=True, slots=True)
class ProcessMemoryInfo:
    current_bytes: int
    peak_bytes: int


def system_memory_status() -> tuple[int, int]:
    """Return total and currently available physical RAM without psutil."""
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
    return 0, 0


def process_memory_info() -> ProcessMemoryInfo:
    try:
        if platform.system() == "Windows":
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
                current = int(counters.WorkingSetSize or counters.PrivateUsage or 0)
                peak = int(counters.PeakWorkingSetSize or current)
                return ProcessMemoryInfo(current, max(current, peak))

        proc_statm = "/proc/self/statm"
        current = 0
        if os.path.isfile(proc_statm):
            with open(proc_statm, "r", encoding="ascii") as stream:
                fields = stream.read().split()
            if len(fields) >= 2:
                current = int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
        import resource
        peak_raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        peak = peak_raw if platform.system() == "Darwin" else peak_raw * 1024
        return ProcessMemoryInfo(max(0, current), max(current, peak))
    except (AttributeError, OSError, ValueError, ImportError):
        return ProcessMemoryInfo(0, 0)


def trim_process_working_set() -> bool:
    """Ask Windows to drop reclaimable working-set pages after a heavy task."""
    if platform.system() != "Windows":
        return False
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
        kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        minus_one = ctypes.c_size_t(-1).value
        return bool(kernel32.SetProcessWorkingSetSize(handle, minus_one, minus_one))
    except (AttributeError, OSError, ValueError):
        return False
