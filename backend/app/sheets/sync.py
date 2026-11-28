"""Состояние встроенной таблицы заданий.

Excel и Google Sheets больше не используются: задания и результаты живут в
SQLite и редактируются прямо в окне программы. Класс сохранён с прежним
интерфейсом (``cycle``/``status``/``push_now``/``append_order``), чтобы
Supervisor и локальный API не пришлось переписывать.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.events import EventBus
from ..db.database import Database


class SyncEngine:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus
        self.state: dict[str, Any] = {
            "backend": "db",
            "mode": "встроенная таблица",
            "enabled": True,
            "last_pull": None,
            "last_push": None,
            "pulled": 0,
            "pushed": 0,
            "last_error": None,
            "interval": 30,
        }

    # ---------- публичное ----------

    def status(self) -> dict[str, Any]:
        return dict(self.state)

    async def probe(self) -> dict[str, Any]:
        return {"ok": True, "mode": "встроенная таблица", "title": "Задания в приложении"}

    async def cycle(self) -> dict[str, Any]:
        """Обновляет счётчики для панели. Внешних таблиц больше нет."""
        progress = await self.db.progress()
        self.state.update(
            {
                "last_pull": datetime.now().strftime("%H:%M:%S"),
                "pulled": progress["tasks"],
                "pushed": progress["done"],
                "last_error": None,
            }
        )
        await self.bus.publish("sync_state", self.status())
        return {"ok": True, "pulled": progress["tasks"], "pushed": progress["done"]}

    async def push_now(self) -> None:
        """Совместимость: результаты уже лежат в SQLite."""
        return None

    async def append_order(self, task: dict[str, Any], position: int | None = None) -> None:
        return None

    def start_loop(self) -> None:
        return None

    async def stop(self) -> None:
        return None
