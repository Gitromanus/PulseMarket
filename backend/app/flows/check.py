"""M2: проверочный сценарий «профиль → прокси → страница».

Реальная выдача Ozon и карточка появятся в M3; здесь важно доказать, что
браузер поднимается с нужным профилем, ходит по сети и окно управляется.
"""

from __future__ import annotations

import random
from typing import Any

from .. import human
from ..browser.session import BrowserSession
from ..core.config import AppSettings
from ..core.events import EventBus


async def run_check(
    session: BrowserSession,
    url: str,
    bus: EventBus,
    thread_id: int,
    settings: AppSettings,
) -> dict[str, Any]:
    page = session.page

    await bus.log(thread_id, f"Открываем {url}")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    title = await page.title()
    await bus.log(thread_id, f"Страница загружена: «{title[:80]}»")

    await human.pause(0.5, 1.2)
    moved = await human.scroll(page, random.randint(3, 6))
    await bus.log(thread_id, f"Проскроллили {moved}px")
    await human.read(page, 2.0, min(8.0, float(settings.view.card_time_sec[1])))
    await bus.log(thread_id, "Просмотр завершён")

    # Окно могло появиться позже навигации — обновляем HWND перед отчётом.
    session.refresh_window()

    return {
        "title": title,
        "url": page.url,
        "status": response.status if response is not None else None,
        "scrolled": moved,
    }
