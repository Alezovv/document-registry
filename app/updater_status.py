from __future__ import annotations

import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any

from app import __version__ as UPDATER_VERSION
from app.update_history import append_update_history


logger = logging.getLogger("DocumentRegistryUpdater")


class NativeUpdaterWindow:
    """Dependency-free Win32 progress window for the standalone updater.

    The updater is a one-file executable and must keep running while the main
    application's ``_internal`` directory is replaced, so this window cannot
    depend on the main PySide6 runtime.  The GUI owns a dedicated message-pump
    thread.  ``start()`` returns True only after Windows reports the HWND as
    visible; merely creating the class/handle is not treated as success.
    """

    WIDTH = 590
    HEIGHT = 205

    def __init__(self, version: str):
        self.version = str(version or "")
        self._queue: queue.Queue[tuple[str, str, int]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._created = False
        self._error = ""

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._created)

    @property
    def error(self) -> str:
        return self._error

    def start(self, timeout_seconds: float = 4.0) -> bool:
        if os.name != "nt":
            return False
        if self.active:
            return True
        if self._thread and self._thread.is_alive():
            self._ready.wait(max(0.1, float(timeout_seconds)))
            return self._created

        self._ready.clear()
        self._created = False
        self._error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="UpdaterStatusWindow",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(max(0.1, float(timeout_seconds)))
        return self._created

    def update(self, message: str, progress: int) -> None:
        if self.active:
            self._queue.put(("update", str(message), max(0, min(100, int(progress)))))

    def close(self, *, delay_seconds: float = 0.0) -> None:
        if self.active:
            self._queue.put(("close", "", int(max(0.0, delay_seconds) * 1000)))

    @staticmethod
    def _last_win32_error(kernel32, action: str) -> OSError:
        try:
            code = int(kernel32.GetLastError())
        except Exception:
            code = 0
        detail = ctypes.FormatError(code).strip() if code else "unknown error"
        return OSError(f"{action} failed ({code}: {detail})")

    def _run(self) -> None:  # pragma: no cover - exercised by frozen Windows E2E
        hwnd = None
        created_ok = False
        user32 = None
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # Python's ctypes.wintypes is intentionally incomplete and differs
            # between Python releases.  In particular Python 3.13 may not expose
            # HCURSOR/HICON/HBRUSH/HINSTANCE.  Win32 declares all of these as
            # opaque handles, so HANDLE/c_void_p is the correct portable fallback.
            HANDLE = getattr(wintypes, "HANDLE", ctypes.c_void_p)
            HWND = getattr(wintypes, "HWND", HANDLE)
            UINT = getattr(wintypes, "UINT", ctypes.c_uint)
            WPARAM = getattr(wintypes, "WPARAM", ctypes.c_size_t)
            LPARAM = getattr(wintypes, "LPARAM", ctypes.c_ssize_t)
            HMENU = getattr(wintypes, "HMENU", HANDLE)
            ATOM = getattr(wintypes, "ATOM", ctypes.c_ushort)
            HICON = getattr(wintypes, "HICON", HANDLE)
            HCURSOR = getattr(wintypes, "HCURSOR", HANDLE)
            HBRUSH = getattr(wintypes, "HBRUSH", HANDLE)
            HINSTANCE = getattr(wintypes, "HINSTANCE", HANDLE)
            HMODULE = getattr(wintypes, "HMODULE", HANDLE)
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", HINSTANCE),
                    ("hIcon", HICON),
                    ("hCursor", HCURSOR),
                    ("hbrBackground", HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            class MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", HWND),
                    ("message", UINT),
                    ("wParam", WPARAM),
                    ("lParam", LPARAM),
                    ("time", wintypes.DWORD),
                    ("pt", wintypes.POINT),
                ]

            user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
            user32.DefWindowProcW.restype = LRESULT
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                HWND,
                HMENU,
                HINSTANCE,
                ctypes.c_void_p,
            ]
            user32.CreateWindowExW.restype = HWND
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype = ATOM
            user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.UpdateWindow.argtypes = [HWND]
            user32.UpdateWindow.restype = wintypes.BOOL
            user32.SetWindowTextW.argtypes = [HWND, wintypes.LPCWSTR]
            user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT, UINT]
            user32.PeekMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
            user32.DispatchMessageW.restype = LRESULT
            user32.DestroyWindow.argtypes = [HWND]
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.MoveWindow.argtypes = [
                HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.BOOL,
            ]
            user32.GetSysColorBrush.argtypes = [ctypes.c_int]
            user32.GetSysColorBrush.restype = HBRUSH
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            user32.IsWindowVisible.argtypes = [HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = HMODULE
            kernel32.GetLastError.restype = wintypes.DWORD

            WM_CLOSE = 0x0010
            WM_DESTROY = 0x0002
            PM_REMOVE = 0x0001
            WM_QUIT = 0x0012

            WS_OVERLAPPED = 0x00000000
            WS_CAPTION = 0x00C00000
            WS_SYSMENU = 0x00080000
            WS_MINIMIZEBOX = 0x00020000
            WS_CHILD = 0x40000000
            WS_VISIBLE = 0x10000000
            WS_EX_TOPMOST = 0x00000008
            WS_EX_APPWINDOW = 0x00040000
            SS_ETCHEDFRAME = 0x00000012
            SS_GRAYRECT = 0x00000005
            SW_SHOW = 5
            SW_MINIMIZE = 6
            COLOR_WINDOW = 5
            SM_CXSCREEN = 0
            SM_CYSCREEN = 1

            @WNDPROC
            def wndproc(window, msg, wparam, lparam):
                # Once the old application exits, cancelling midway through file
                # replacement would be unsafe.  Close therefore means minimise.
                if msg == WM_CLOSE:
                    user32.ShowWindow(window, SW_MINIMIZE)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(window, msg, wparam, lparam)

            hinstance = kernel32.GetModuleHandleW(None)
            class_name = f"DocumentRegistryUpdaterStatus_{os.getpid()}"
            wc = WNDCLASSW()
            wc.lpfnWndProc = wndproc
            wc.hInstance = hinstance
            wc.hbrBackground = user32.GetSysColorBrush(COLOR_WINDOW)
            wc.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise self._last_win32_error(kernel32, "RegisterClassW")

            style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
            screen_width = max(self.WIDTH, user32.GetSystemMetrics(SM_CXSCREEN))
            screen_height = max(self.HEIGHT, user32.GetSystemMetrics(SM_CYSCREEN))
            x = max(0, int((screen_width - self.WIDTH) / 2))
            y = max(0, int((screen_height - self.HEIGHT) / 2))
            hwnd = user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_APPWINDOW,
                class_name,
                f"Установка DocumentRegistry {self.version}",
                style,
                x,
                y,
                self.WIDTH,
                self.HEIGHT,
                None,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise self._last_win32_error(kernel32, "CreateWindowExW")

            # For predefined system controls hInstance is intentionally NULL.
            # This is the documented, least fragile form for STATIC controls.
            child_instance = None
            title = user32.CreateWindowExW(
                0,
                "STATIC",
                "Установка обновления DocumentRegistry",
                WS_CHILD | WS_VISIBLE,
                24,
                22,
                535,
                24,
                hwnd,
                None,
                child_instance,
                None,
            )
            status = user32.CreateWindowExW(
                0,
                "STATIC",
                "Подготовка установки...",
                WS_CHILD | WS_VISIBLE,
                24,
                57,
                535,
                34,
                hwnd,
                None,
                child_instance,
                None,
            )
            progress_frame = user32.CreateWindowExW(
                0,
                "STATIC",
                "",
                WS_CHILD | WS_VISIBLE | SS_ETCHEDFRAME,
                24,
                108,
                535,
                22,
                hwnd,
                None,
                child_instance,
                None,
            )
            progress_fill = user32.CreateWindowExW(
                0,
                "STATIC",
                "",
                WS_CHILD | WS_VISIBLE | SS_GRAYRECT,
                26,
                110,
                10,
                18,
                hwnd,
                None,
                child_instance,
                None,
            )
            percent_label = user32.CreateWindowExW(
                0,
                "STATIC",
                "0%",
                WS_CHILD | WS_VISIBLE,
                510,
                137,
                48,
                22,
                hwnd,
                None,
                child_instance,
                None,
            )

            if not all((title, status, progress_frame, progress_fill, percent_label)):
                raise self._last_win32_error(kernel32, "CreateWindowExW(STATIC)")

            user32.ShowWindow(hwnd, SW_SHOW)
            user32.UpdateWindow(hwnd)
            # Best effort only: Windows may reject foreground activation, but the
            # TOPMOST + APPWINDOW flags still make the installer discoverable.
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            if not user32.IsWindowVisible(hwnd):
                raise OSError("Updater HWND was created but Windows reports it as hidden")

            created_ok = True
            self._created = True
            self._ready.set()

            message = MSG()
            close_at: float | None = None
            running = True
            while running:
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    if message.message == WM_QUIT:
                        running = False
                        break
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))

                try:
                    command, text, value = self._queue.get(timeout=0.05)
                except queue.Empty:
                    command = ""
                    text = ""
                    value = 0

                if command == "update":
                    user32.SetWindowTextW(status, text)
                    user32.SetWindowTextW(percent_label, f"{value}%")
                    inner_width = 531
                    fill_width = max(2, int(inner_width * value / 100.0))
                    user32.MoveWindow(progress_fill, 26, 110, fill_width, 18, True)
                elif command == "close":
                    close_at = time.monotonic() + (value / 1000.0)

                if close_at is not None and time.monotonic() >= close_at:
                    user32.DestroyWindow(hwnd)
                    running = False
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            logger.exception("Не удалось создать окно прогресса установки")
            if hwnd and user32 is not None and not created_ok:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
        finally:
            if not self._ready.is_set():
                self._ready.set()
            self._created = False


class ConsoleUpdaterWindow:
    """Last-resort visible installer status using a Windows console window."""

    def __init__(self, version: str):
        self.version = str(version or "")
        self._started = False
        self._allocated_here = False
        self._handle = None
        self._error = ""

    @property
    def active(self) -> bool:
        return self._started

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> bool:  # pragma: no cover - Windows frozen fallback
        if os.name != "nt":
            return False
        try:
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            HANDLE = getattr(wintypes, "HANDLE", ctypes.c_void_p)
            HWND = getattr(wintypes, "HWND", HANDLE)
            BOOL = getattr(wintypes, "BOOL", ctypes.c_int)
            kernel32.GetConsoleWindow.restype = HWND
            kernel32.AllocConsole.restype = BOOL
            kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetStdHandle.restype = HANDLE
            existing = kernel32.GetConsoleWindow()
            if not existing:
                if not kernel32.AllocConsole():
                    raise OSError(f"AllocConsole failed: {ctypes.FormatError(kernel32.GetLastError())}")
                self._allocated_here = True
            kernel32.SetConsoleTitleW(f"Установка DocumentRegistry {self.version}")
            self._handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            handle_value = getattr(self._handle, "value", self._handle)
            if not handle_value or int(handle_value) in {-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}:
                raise OSError("GetStdHandle(STD_OUTPUT_HANDLE) failed")
            self._started = True
            self._write("Установка обновления DocumentRegistry\r\n")
            self._write("Окно установки запущено в резервном режиме.\r\n\r\n")
            try:
                hwnd = kernel32.GetConsoleWindow()
                if hwnd:
                    user32.ShowWindow(hwnd, 5)
                    user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            return True
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            logger.exception("Не удалось создать резервное консольное окно updater")
            self._started = False
            return False

    def _write(self, text: str) -> None:  # pragma: no cover - Windows frozen fallback
        if not self._started or not self._handle:
            return
        try:
            from ctypes import wintypes

            written = wintypes.DWORD(0)
            kernel32 = ctypes.windll.kernel32
            kernel32.WriteConsoleW.argtypes = [
                getattr(wintypes, "HANDLE", ctypes.c_void_p),
                ctypes.c_wchar_p,
                getattr(wintypes, "DWORD", ctypes.c_ulong),
                ctypes.POINTER(getattr(wintypes, "DWORD", ctypes.c_ulong)),
                ctypes.c_void_p,
            ]
            kernel32.WriteConsoleW.restype = wintypes.BOOL
            value = str(text)
            kernel32.WriteConsoleW(
                self._handle,
                value,
                len(value),
                ctypes.byref(written),
                None,
            )
        except Exception:
            logger.debug("Не удалось обновить резервное окно updater", exc_info=True)

    def update(self, message: str, progress: int) -> None:
        if self.active:
            self._write(f"[{max(0, min(100, int(progress))):3d}%] {message}\r\n")

    def close(self, *, delay_seconds: float = 0.0) -> None:
        if not self.active:
            return
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            if self._allocated_here:
                ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass
        self._started = False


class UpdaterDisplay:
    """Prefer the native GUI, but guarantee a visible console fallback."""

    def __init__(self, version: str):
        self.version = str(version or "")
        self.native = NativeUpdaterWindow(self.version)
        self.console = ConsoleUpdaterWindow(self.version)
        self.mode = "none"
        self.error = ""

    @property
    def active(self) -> bool:
        if self.mode == "native":
            return self.native.active
        if self.mode == "console":
            return self.console.active
        return False

    def start(self) -> bool:
        if self.native.start():
            self.mode = "native"
            return True
        native_error = self.native.error
        if self.console.start():
            self.mode = "console"
            self.error = native_error
            logger.warning("Используется резервное консольное окно updater: %s", native_error)
            return True
        self.error = "; ".join(
            part for part in (native_error, self.console.error) if part
        ) or "Не удалось создать ни графическое, ни консольное окно."
        return False

    def update(self, message: str, progress: int) -> None:
        if self.mode == "native":
            self.native.update(message, progress)
        elif self.mode == "console":
            self.console.update(message, progress)

    def close(self, *, delay_seconds: float = 0.0) -> None:
        if self.mode == "native":
            self.native.close(delay_seconds=delay_seconds)
        elif self.mode == "console":
            self.console.close(delay_seconds=delay_seconds)


class UpdaterStatusReporter:
    """Durable install state plus a guaranteed visible installer display."""

    FILE_NAME = "update_status.json"

    def __init__(self, target: Path, version: str, *, show_window: bool = True, rollback: bool = False):
        self.target = Path(target)
        self.version = str(version or "")
        self.rollback = bool(rollback)
        self.path = self.target / "data" / self.FILE_NAME
        self.session_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        self.window = UpdaterDisplay(self.version)
        self.show_window = bool(show_window and os.name == "nt" and getattr(sys, "frozen", False))
        self.window_started = False
        self.window_mode = "none"
        if self.show_window:
            self.window_started = self.window.start()
            self.window_mode = self.window.mode
            if not self.window_started:
                logger.error(
                    "Окно установки не было создано%s",
                    f": {self.window.error}" if self.window.error else "",
                )

    def _write(self, state: str, stage: str, message: str, progress: int) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "format_version": 1,
                "state": state,
                "stage": stage,
                "message": str(message),
                "progress": max(0, min(100, int(progress))),
                "version": self.version,
                "rollback": self.rollback,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "notified": False,
                "window_started": self.window_started,
                "window_mode": self.window_mode,
                "updater_version": str(UPDATER_VERSION),
                "updater_executable": str(Path(sys.executable).resolve(strict=False)),
            }
            if self.show_window and not self.window_started and self.window.error:
                payload["window_error"] = self.window.error
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
            history_payload = dict(payload)
            history_payload["session_id"] = self.session_id
            history_payload["timestamp"] = payload["updated_at"]
            history_payload.pop("notified", None)
            append_update_history(self.path.parent, history_payload)
        except OSError:
            pass

    def stage(self, stage: str, message: str, progress: int) -> None:
        self._write("installing", stage, message, progress)
        self.window.update(message, progress)

    def success(self, message: str = "Обновление установлено. Запускаем программу...") -> None:
        self._write("success", "done", message, 100)
        self.window.update(message, 100)

    def failed(self, message: str) -> None:
        self._write("failed", "failed", message, 100)
        self.window.update(message, 100)

    def close(self, *, success: bool = False) -> None:
        self.window.close(delay_seconds=0.9 if success else 0.0)
