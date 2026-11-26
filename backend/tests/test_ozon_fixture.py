"""Офлайн-прогон сценария Ozon на реальном DOM, сохранённом в fixtures.

Фикстуры не хранятся в репозитории (это выгрузка чужой страницы). Как получить:

    cd backend
    ./.venv/Scripts/python.exe tools/capture_ozon.py "наушники"

Тест пропускается, если фикстур нет или они устарели.
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
from app.flows import ozon as ozon_module  # noqa: E402
from app.flows.ozon import _collect_cards, _open_search, find_position, run_ozon  # noqa: E402

# Офлайн-фикстура: вход через главную Ozon тут недоступен.
ozon_module.HUMAN_SEARCH = False

FIXTURES = Path(__file__).parent / "fixtures" / "ozon"
SEARCH = FIXTURES / "search.html"
CARD = FIXTURES / "card.html"


async def main() -> None:
    if not SEARCH.exists() or not CARD.exists():
        print(f"fixtures: пропущено — нет {FIXTURES} (см. tools/capture_ozon.py)")
        return

    settings = AppSettings()
    settings.browser.headless = True
    settings.view.card_time_sec = (2, 3)
    settings.view.reviews_time_sec = (1, 1.5)
    settings.view.max_search_pages = 1
    settings.view.like_pct = 100
    settings.view.cart_pct = 100
    settings.view.compare_pct = 100
    settings.view.review_like_pct = 0
    settings.view.see_also_pct = 0
    settings.view.other_sellers_pct = 0
    settings.general.quick_actions.view_photos = False

    bus = EventBus()
    provider = VanillaProvider(settings.browser)
    profile = Profile(Path(tempfile.mkdtemp()) / "p", kind="noauth", id="p")
    session = await provider.open(profile, None)

    async def handler(route) -> None:  # type: ignore[no-untyped-def]
        url = route.request.url
        if "/search/" in url:
            await route.fulfill(path=str(SEARCH), content_type="text/html; charset=utf-8")
        elif "/product/" in url:
            await route.fulfill(path=str(CARD), content_type="text/html; charset=utf-8")
        else:
            await route.abort()

    try:
        page = session.page
        await page.route("**/*", handler)

        await _open_search(page, "наушники", 1, bus, 0)
        cards = await _collect_cards(page)
        assert len(cards) >= 20, f"карточек в фикстуре: {len(cards)}"
        first_sku = next(iter(cards))

        position, page_no, href = await find_position(page, "наушники", first_sku, 1, bus, 0)
        assert position == 1 and page_no == 1 and href, (position, page_no, href)

        second_sku = list(cards)[1]
        position2, _, _ = await find_position(page, "наушники", second_sku, 1, bus, 0)
        assert position2 == 2, f"второй товар должен быть на позиции 2, получили {position2}"

        task = {
            "id": 1,
            "sku": first_sku,
            "query": "наушники",
            "geo": "",
            "marketplace": "ozon",
            "sheet_row": 2,
        }
        result = await run_ozon(session, task, settings, bus, 0)
        assert result["position"] == 1, result
        assert result["cart"] == 1, "кнопка корзины не отработала"
        assert result["liked"] == 1, "кнопка избранного не отработала"
        steps = {step["step"] for step in result["steps"]}
        assert {"position", "card_opened", "favorite", "cart"} <= steps, steps
    finally:
        await provider.close(session)
        await shutdown_playwright()

    print("ozon: ok — выдача из фикстуры, позиция 1/2, карточка, избранное, корзина")


if __name__ == "__main__":
    asyncio.run(main())
