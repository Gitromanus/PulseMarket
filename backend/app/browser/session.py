"""Обёртка над запущенной сессией браузера."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import win_window
from .profiles import Profile
from .proxies import Proxy


@dataclass
class BrowserSession:
    kind: str
    context: Any
    page: Any
    profile: Profile
    proxy: Proxy | None = None
    pid: int | None = None
    hwnd: int | None = None
    external: bool = False
    visible: bool = True
    closing: bool = False
    fingerprint: str = ""
    _pids: list[int] = field(default_factory=list)

    def refresh_window(self) -> int | None:
        """HWND ищем один раз: после SW_HIDE окно существует, но уже не видимо.

        Если переискать «по видимости», можно подхватить служебное окно и
        показывать его вместо браузера.
        """
        if self.hwnd and win_window.is_window(self.hwnd):
            return self.hwnd
        self.hwnd = None
        for pid in [self.pid, *self._pids]:
            if not pid:
                continue
            candidate = win_window.find_browser_hwnd(pid)
            if candidate:
                self.hwnd = candidate
                return candidate
        return self.hwnd

    def set_visible(self, visible: bool) -> bool:
        if self.external:
            return False
        self.refresh_window()
        if not self.hwnd:
            return False
        actual = win_window.set_visible(self.hwnd, visible)
        self.visible = actual
        return actual == visible

    @property
    def has_window(self) -> bool:
        return bool(self.hwnd or self.refresh_window())

    def info(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "profile": self.profile.label,
            "profile_kind": self.profile.kind,
            "proxy": self.proxy.masked() if self.proxy else "direct",
            "pid": self.pid,
            "hwnd": self.hwnd,
            "visible": self.visible,
            "url": getattr(self.page, "url", ""),
        }
