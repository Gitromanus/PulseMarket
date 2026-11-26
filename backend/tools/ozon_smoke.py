"""Живой прогон Ozon без очереди и интерфейса.

python tools/ozon_smoke.py [запрос]

Проверяет: выдачу, сбор карточек, позицию, карточку и действия.
Тайминги укорочены, чтобы прогон занимал минуту, а не пять.
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
from app.flows.base import ScenarioError  # noqa: E402
from app.flows.ozon import _collect_cards, _open_search, find_position, run_ozon  # noqa: E402

QUERY = sys.argv[1] if len(sys.argv) > 1 else "наушники"


async def main() -> None:
    settings = AppSettings()
    settings.browser.headless = True
    settings.view.card_time_sec = (30, 45)
    settings.view.reviews_time_sec = (5, 8)
    settings.view.max_search_pages = 2
    settings.view.like_pct = 100
    settings.view.cart_pct = 100
    settings.view.review_like_pct = 0
    settings.view.compare_pct = 0
    settings.view.see_also_pct = 0
    settings.view.other_sellers_pct = 0

    bus = EventBus()
    provider = VanillaProvider(settings.browser)
    profile = Profile(folder=Path(tempfile.mkdtemp()) / "smoke", kind="noauth", id="smoke")
    session = await provider.open(profile, None)
    try:
        page = session.page
        await _open_search(page, QUERY, 1, bus, 0)
        cards = await _collect_cards(page)
        print(f"cards={len(cards)}")
        if not cards:
            raise SystemExit("выдача пуста")

        sku, href = next(iter(cards.items()))
        position, page_no, href2 = await find_position(page, QUERY, sku, 2, bus, 0)
        print(f"position={position} page={page_no} sku={sku}")
        assert position == 1, f"первый товар выдачи должен быть на позиции 1, получили {position}"

        task = {
            "id": 999,
            "sku": sku,
            "query": QUERY,
            "geo": "",
            "marketplace": "ozon",
            "sheet_row": 2,
        }
        result = await run_ozon(session, task, settings, bus, 0)
        print(
            "result="
            + str({k: result[k] for k in ("position", "page", "cart", "liked", "dwell")})
        )
        print("steps=" + ",".join(step["step"] for step in result["steps"]))
    finally:
        await provider.close(session)
        await shutdown_playwright()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ScenarioError as error:
        print(f"SMOKE FAILED: {error.code}: {error}")
        if error.code == "ozon_blocked":
            print("Подсказка: площадка ограничила IP. Нужен residential-прокси или пауза, код при этом верный.")
        raise SystemExit(1) from error
