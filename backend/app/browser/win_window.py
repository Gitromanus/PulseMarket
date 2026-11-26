"""Показ/скрытие окна браузера на Windows.

Playwright не умеет встраивать Chromium в Electron, поэтому «Показать браузер»
работает через Win32: находим HWND верхнего окна процесса и переключаем видимость.
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

CHROME_CLASSES = {"Chrome_WidgetWin_0", "Chrome_WidgetWin_1"}

if IS_WINDOWS:
    import win32con
    import win32gui
    import win32process


def _enum_windows() -> list[tuple[int, int, str]]:
    if not IS_WINDOWS:
        return []
    found: list[tuple[int, int, str]] = []

    def callback(hwnd: int, _: object) -> bool:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            found.append((hwnd, pid, win32gui.GetClassName(hwnd)))
        except Exception:  # noqa: BLE001 — окно могло закрыться
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return found


def find_browser_hwnd(pid: int) -> int | None:
    """Самое крупное окно Chrome указанного процесса (и скрытые тоже видны энумератору)."""
    if not IS_WINDOWS:
        return None
    best: tuple[int, int, int] | None = None  # (visible, area, hwnd)
    for hwnd, win_pid, cls in _enum_windows():
        if win_pid != pid or cls not in CHROME_CLASSES:
            continue
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            visible = 1 if win32gui.IsWindowVisible(hwnd) else 0
        except Exception:  # noqa: BLE001
            continue
        area = max(0, right - left) * max(0, bottom - top)
        if area <= 0:
            continue
        candidate = (visible, area, hwnd)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2] if best else None


def is_visible(hwnd: int | None) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(win32gui.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def is_window(hwnd: int | None) -> bool:
    """Окно ещё существует (в отличие от is_visible, скрытое тоже считается)."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def set_visible(hwnd: int | None, visible: bool) -> bool:
    """Возвращает фактическое состояние после переключения."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW if visible else win32con.SW_HIDE)
        if visible:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001 — фокус мог перехватить другой процесс
                pass
        return is_visible(hwnd)
    except Exception:  # noqa: BLE001
        return False
