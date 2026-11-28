"""Провайдеры браузера: vanilla Chromium, CDP к антидетекту, Camoufox (заглушка)."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Protocol

import psutil

from ..core.config import BrowserSettings
from . import win_window
from .fingerprints import device_for, spoof_hardware_enabled
from .profiles import Profile
from .proxies import Proxy
from .session import BrowserSession


# Следы Playwright в window, которые ищут антиботы. Патчить navigator.webdriver
# вручную нельзя: defineProperty оставляет не-[native code] след.
STEALTH_INIT = r"""
try {
  delete window.playwright;
  delete window.playwright$;
} catch (e) {}
"""

def _device_options(profile: Profile) -> dict[str, Any]:
    """Экран, окно и DPR стабильны для профиля, но различаются между профилями."""
    return device_for(profile.id).playwright()


class ProviderError(RuntimeError):
    pass


class BrowserProvider(Protocol):
    kind: str

    async def open(self, profile: Profile, proxy: Proxy | None) -> BrowserSession: ...

    async def close(self, session: BrowserSession) -> None: ...


# ---------- общий Playwright ----------

_playwright: Any = None
_pw_lock = asyncio.Lock()


async def get_playwright() -> Any:
    global _playwright
    if _playwright is None:
        async with _pw_lock:
            if _playwright is None:
                async_playwright = _engine_launcher()
                try:
                    _playwright = await async_playwright().start()
                except Exception as exc:  # noqa: BLE001
                    raise ProviderError(f"Браузерный движок не запустился: {exc}") from exc
    return _playwright


def _engine_launcher() -> Any:
    """patchright (по умолчанию, если установлен) убирает CDP-следы.

    `PF_ENGINE=playwright` принудительно возвращает обычный Playwright —
    аварийный вариант, если patchright не запускается.
    """
    preferred = os.getenv("PF_ENGINE", "auto").strip().lower()
    if preferred != "playwright":
        try:
            from patchright.async_api import async_playwright  # type: ignore[import-not-found]

            return async_playwright
        except ImportError:
            if preferred == "patchright":
                raise ProviderError(
                    "PF_ENGINE=patchright, но пакет не установлен: pip install patchright"
                ) from None
    from playwright.async_api import async_playwright

    return async_playwright


def engine_name() -> str:
    if os.getenv("PF_ENGINE", "auto").strip().lower() == "playwright":
        return "playwright"
    try:
        import patchright  # noqa: F401

        return "patchright"
    except ImportError:
        return "playwright"


def _chrome_installed() -> bool:
    for base in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
        if not base:
            continue
        if os.path.exists(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")):
            return True
    return False


def _edge_installed() -> bool:
    for base in (os.getenv("PROGRAMFILES(X86)"), os.getenv("PROGRAMFILES"), os.getenv("LOCALAPPDATA")):
        if not base:
            continue
        if os.path.exists(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")):
            return True
    return False


async def shutdown_playwright() -> None:
    global _playwright
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        _playwright = None


def _browser_pids(marker: str) -> list[int]:
    marker = marker.lower()
    pids: list[int] = []
    try:
        for child in psutil.Process().children(recursive=True):
            try:
                name = (child.name() or "").lower()
                if "chrom" not in name and "headless_shell" not in name:
                    continue
                cmdline = " ".join(child.cmdline()).lower()
                if marker in cmdline:
                    pids.append(child.pid)
            except (psutil.Error, OSError):
                continue
    except psutil.Error:
        pass
    return pids


def clear_stale_service_workers(folder: Any) -> None:
    """Убирает Service Worker из профиля перед запуском браузера.

    Старый SW (например, Ozon) перехватывает переходы и отдаёт закэшированный
    неуспешный ответ: страница падает с ERR_HTTP_RESPONSE_CODE_FAILURE / 407,
    хотя прокси рабочий. На чистом профиле та же ссылка открывается нормально.
    Кэш SW — не часть отпечатка, поэтому чистим его без потери личности.
    """
    target = Path(folder) / "Default" / "Service Worker"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


class VanillaProvider:
    kind = "vanilla"

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings

    async def open(self, profile: Profile, proxy: Proxy | None) -> BrowserSession:
        clear_stale_service_workers(profile.folder)
        pw = await get_playwright()
        device = device_for(profile.id)
        options: dict[str, Any] = {
            "headless": self.settings.headless,
            "locale": self.settings.locale,
            # --test-type отключает жёлтый инфобар «неподдерживаемый флаг»
            # (сайту этот флаг не виден).
            "args": ["--test-type"],
            **device.playwright(),
        }
        if engine_name() == "playwright":
            # У patchright это уже сделано внутри; добавлять флаги ему нельзя —
            # Chrome показывает баннер «неподдерживаемый флаг» и это маркер.
            options["args"] = [
                "--test-type",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                f"--lang={self.settings.locale}",
            ]
            options["ignore_default_args"] = [
                "--enable-automation",
                "--no-sandbox",
                "--disable-infobars",
                "--enable-features=CDPScreenshotNewSurface",
                "--hide-scrollbars",
                "--mute-audio",
                "--force-color-profile=srgb",
                "--disable-dev-shm-usage",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
            if os.getenv("PF_NO_SANDBOX") != "1":
                options["chromium_sandbox"] = True
        # Без песочницы Chrome показывает баннер про --no-sandbox. Patchright
        # добавляет этот флаг сам, поэтому включаем песочницу для обоих движков.
        if os.getenv("PF_NO_SANDBOX") != "1":
            options["chromium_sandbox"] = True
        # Пусто — не передаём: Chromium возьмёт пояс из ОС (как система и IP).
        if self.settings.timezone:
            options["timezone_id"] = self.settings.timezone
        if proxy is not None:
            options["proxy"] = proxy.playwright()

        channel = (os.getenv("PF_BROWSER_CHANNEL") or self.settings.channel).strip().lower()
        if channel == "edge":
            channel = "msedge"
        if channel == "auto":
            # Chrome, затем Edge (есть на любой Windows), в крайнем случае Chromium.
            if _chrome_installed():
                channel = "chrome"
            elif _edge_installed():
                channel = "msedge"
            else:
                channel = "chromium"
        if channel in ("chrome", "msedge"):
            options["channel"] = channel

        try:
            context = await pw.chromium.launch_persistent_context(str(profile.folder), **options)
        except Exception as exc:  # noqa: BLE001
            if "channel" in options:
                # Chrome мог обновиться/не открыть профиль — откатываемся на Chromium.
                print(f"[browser] Chrome не запустился, откат на Chromium: {exc}", file=sys.stderr)
                options.pop("channel", None)
                try:
                    context = await pw.chromium.launch_persistent_context(
                        str(profile.folder), **options
                    )
                except Exception as exc2:  # noqa: BLE001
                    raise ProviderError(
                        f"Браузер не запустился: {str(exc2).splitlines()[0][:300]}"
                    ) from exc2
            else:
                raise ProviderError(
                    f"Браузер не запустился: {str(exc).splitlines()[0][:300]}"
                ) from exc

        try:
            await context.add_init_script(STEALTH_INIT)
        except Exception:  # noqa: BLE001 — патч не должен мешать запуску
            pass
        hardware_spoofed = False
        # Patchright намеренно не выполняет init-скрипты (проверено): подмена
        # hardwareConcurrency/deviceMemory работала бы только в plain Playwright.
        if spoof_hardware_enabled() and engine_name() == "playwright":
            try:
                await context.add_init_script(device.init_script())
                hardware_spoofed = True
            except Exception:  # noqa: BLE001
                pass
        page = context.pages[0] if context.pages else await context.new_page()
        session = BrowserSession(
            kind=self.kind,
            context=context,
            page=page,
            profile=profile,
            proxy=proxy,
            fingerprint=device.describe(hardware_spoofed),
        )

        if not self.settings.headless:
            # Окно появляется не мгновенно: до 3 секунд ищем процесс и HWND.
            for _ in range(15):
                pids = await asyncio.to_thread(_browser_pids, str(profile.folder))
                if pids:
                    session._pids = pids
                    session.pid = pids[-1]
                    if session.refresh_window():
                        break
                await asyncio.sleep(0.2)
            for _ in range(10):
                if win_window.is_visible(session.hwnd):
                    break
                await asyncio.sleep(0.2)
            session.visible = win_window.is_visible(session.hwnd)

        if os.getenv("PF_BROWSER_TRACE"):
            page.on(
                "crash",
                lambda *_: print(f"[browser] page crashed: {profile.label}", file=sys.stderr),
            )
            context.on(
                "close",
                lambda *_: print(
                    f"[browser] context closed: {profile.label} (ours={session.closing})",
                    file=sys.stderr,
                ),
            )

        return session

    async def close(self, session: BrowserSession) -> None:
        session.closing = True
        try:
            await session.context.close()
        except Exception:  # noqa: BLE001
            pass


class CdpProvider:
    """Подключение к уже запущенному антидетект-браузеру по CDP-эндпоинту."""

    kind = "cdp"

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings

    async def open(self, profile: Profile, proxy: Proxy | None) -> BrowserSession:
        pw = await get_playwright()
        try:
            browser = await pw.chromium.connect_over_cdp(self.settings.cdp_endpoint)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"Нет CDP-подключения к {self.settings.cdp_endpoint}: {str(exc).splitlines()[0][:200]}"
            ) from exc

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        return BrowserSession(
            kind=self.kind,
            context=context,
            page=page,
            profile=profile,
            proxy=proxy,
            external=True,
            visible=True,
        )

    async def close(self, session: BrowserSession) -> None:
        # Не закрываем чужой браузер: только вкладку, и то без падения.
        try:
            await session.page.close()
        except Exception:  # noqa: BLE001
            pass


class CamoufoxProvider:
    kind = "camoufox"

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings

    async def open(self, profile: Profile, proxy: Proxy | None) -> BrowserSession:
        raise ProviderError(
            "Провайдер camoufox ещё не подключён. Установите camoufox и включите его в M3, "
            "либо используйте vanilla/cdp."
        )

    async def close(self, session: BrowserSession) -> None:
        return None


def build_provider(settings: BrowserSettings) -> BrowserProvider:
    if settings.provider == "cdp":
        return CdpProvider(settings)
    if settings.provider == "camoufox":
        return CamoufoxProvider(settings)
    return VanillaProvider(settings)


async def check_chromium() -> dict[str, Any]:
    """Проверка наличия браузера без его запуска."""
    try:
        pw = await get_playwright()
        path = pw.chromium.executable_path
        exists = bool(path) and os.path.exists(path)
        return {"ok": exists, "path": path}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
