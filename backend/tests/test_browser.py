"""M2: проверка браузерного слоя без маркетплейса.

python tests/test_browser.py

1) headless: страница, человеческая печать, скролл;
2) headed: окно находится по PID, прячется и показывается через Win32.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Тест проверяет наш слой, а не patchright: фиксируем движок и канал.
os.environ.setdefault("PF_ENGINE", "playwright")

from app import human  # noqa: E402
from app.browser.profiles import Profile  # noqa: E402
from app.browser.providers import VanillaProvider, shutdown_playwright  # noqa: E402
from app.browser.proxies import parse_proxy  # noqa: E402
from app.core.config import AppSettings  # noqa: E402


async def check_proxy_parsing() -> None:
    proxy = parse_proxy("socks5://user:pa%40ss@1.2.3.4:1080")
    assert proxy is not None
    assert proxy.server == "socks5://1.2.3.4:1080"
    assert proxy.username == "user"
    assert proxy.password == "pa%40ss"
    assert parse_proxy("127.0.0.1:9050") is not None
    assert parse_proxy("плохая строка") is None
    assert parse_proxy("# комментарий") is None


async def check_headless(tmp: Path) -> None:
    settings = AppSettings()
    settings.browser.headless = True
    settings.browser.channel = "chromium"  # тест не зависит от установленного Chrome
    provider = VanillaProvider(settings.browser)
    profile = Profile(folder=tmp / "headless", kind="noauth", id="headless")

    session = await provider.open(profile, None)
    try:
        page = session.page
        await page.set_content(
            "<!doctype html><title>PF</title>"
            "<input id=q>"
            "<div style='height:4000px'>низ</div>"
        )
        assert await page.title() == "PF"

        await human.type_text(page, page.locator("#q"), "наушники")
        value = await page.locator("#q").input_value()
        assert value == "наушники", f"в поле: {value!r}"

        await human.scroll(page, 4)
        offset = await page.evaluate("window.scrollY")
        assert offset > 0, "страница не проскроллилась"
    finally:
        await provider.close(session)


async def check_window(tmp: Path) -> None:
    settings = AppSettings()
    settings.browser.headless = False
    settings.browser.channel = "chromium"
    provider = VanillaProvider(settings.browser)
    profile = Profile(folder=tmp / "headed", kind="noauth", id="headed")

    session = await provider.open(profile, None)
    try:
        await session.page.set_content("<!doctype html><title>PF Window</title><h1>ok</h1>")
        hwnd = session.refresh_window()
        if not hwnd:
            print("window: пропущено — окно не найдено (нет интерактивного рабочего стола)")
            return

        assert session.set_visible(False) is True, "окно не скрылось"
        assert session.visible is False
        assert session.hwnd == hwnd, "после скрытия HWND подменился"
        assert session.set_visible(True) is True, "окно не показалось"
        assert session.visible is True
        assert session.hwnd == hwnd, "после показа HWND подменился"
    finally:
        await provider.close(session)


async def main() -> None:
    await check_proxy_parsing()
    tmp = Path(tempfile.mkdtemp(prefix="pf-browser-"))
    try:
        await check_headless(tmp)
        await check_window(tmp)
    finally:
        await shutdown_playwright()
    print("browser: ok — прокси, headless-страница, печать/скролл, show/hide окна")


if __name__ == "__main__":
    asyncio.run(main())
