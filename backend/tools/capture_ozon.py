"""Сохраняет реальные страницы Ozon в tests/fixtures/ozon для офлайн-теста.

python tools/capture_ozon.py "наушники"

Нужен доступ к Ozon с текущего IP. Если площадка отдаёт 403 — подождите
или используйте прокси из вашего пула.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser.profiles import Profile  # noqa: E402
from app.browser.providers import VanillaProvider, shutdown_playwright  # noqa: E402
from app.core.config import AppSettings  # noqa: E402
from app.core.events import EventBus  # noqa: E402
from app.flows.ozon import _collect_cards, _open_search  # noqa: E402

QUERY = sys.argv[1] if len(sys.argv) > 1 else "наушники"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ozon"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = AppSettings()
    settings.browser.headless = True
    provider = VanillaProvider(settings.browser)
    profile = Profile(Path(tempfile.mkdtemp()) / "capture", kind="noauth", id="capture")
    session = await provider.open(profile, None)
    try:
        page = session.page
        await _open_search(page, QUERY, 1, EventBus(), 0)
        cards = await _collect_cards(page)
        (OUT / "search.html").write_text(await page.content(), encoding="utf-8")
        print(f"search.html сохранён, карточек {len(cards)}")

        sku, href = next(iter(cards.items()))
        absolute = href if href.startswith("http") else f"https://www.ozon.ru{href}"
        await page.goto(absolute, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        for _ in range(4):
            await page.mouse.wheel(0, 900)
            await asyncio.sleep(0.8)
        (OUT / "card.html").write_text(await page.content(), encoding="utf-8")
        print(f"card.html сохранён, sku {sku}")
    finally:
        await provider.close(session)
        await shutdown_playwright()


if __name__ == "__main__":
    asyncio.run(main())
