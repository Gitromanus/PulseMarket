"""Ручной режим (M6): открыть/закрыть сессию, защита занятого профиля.

python tests/test_manual.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser.providers import shutdown_playwright  # noqa: E402
from app.core.config import AppSettings  # noqa: E402
from app.core.events import EventBus  # noqa: E402
from app.core.manual import ManualManager  # noqa: E402


async def scenario() -> None:
    settings = AppSettings()
    settings.browser.headless = True
    manager = ManualManager(EventBus())

    try:
        started = await manager.start(settings, url="about:blank")
        assert started["ok"], started
        item = started["item"]
        assert manager.list(), "сессия не попала в список"

        # headless: окном управлять нельзя, но сессия жива.
        hidden = await manager.set_visible(item["id"], False)
        assert hidden["ok"] is False and "headless" in hidden["error"].lower(), hidden

        moved = await manager.navigate(
            item["id"], "data:text/html,<title>Manual</title><h1>ok</h1>"
        )
        assert moved["ok"], moved
        assert moved["item"]["url"].startswith("data:text/html"), moved["item"]

        closed = await manager.close(item["id"])
        assert closed["ok"] and manager.list() == [], "сессия не закрылась"

        # Занятый профиль открывать нельзя.
        folder = Path(tempfile.mkdtemp(prefix="pf-manual-")) / "busy"
        folder.mkdir()
        busy = await manager.start(
            settings, profile_path=str(folder), url="about:blank", busy_paths={str(folder)}
        )
        assert busy["ok"] is False and "занят" in busy["error"].lower(), busy

        # Несуществующая папка — понятная ошибка, а не трейсбек.
        missing = await manager.start(
            settings, profile_path=str(folder / "nope"), url="about:blank"
        )
        assert missing["ok"] is False and "не найдена" in missing["error"].lower(), missing
    finally:
        await manager.close_all()
        await shutdown_playwright()

    print("manual: ok — старт/навигация/закрытие, защита занятого профиля, понятные ошибки")


if __name__ == "__main__":
    asyncio.run(scenario())
