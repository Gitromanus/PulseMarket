"""Планировщик: запускать прогон по интервалу, если есть задания и очередь свободна."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from ..db.database import Database
from .config import load_settings
from .events import EventBus
from .supervisor import Supervisor


class Scheduler:
    def __init__(
        self,
        sup: Supervisor,
        db: Database,
        bus: EventBus,
        settings_provider: Callable[[], Any] = load_settings,
    ) -> None:
        self.sup = sup
        self.db = db
        self.bus = bus
        self._settings = settings_provider
        self._task: asyncio.Task | None = None
        self._next_run: datetime | None = None
        self._last_run: datetime | None = None
        self._last_result: str | None = None

    # ---------- состояние ----------

    def state(self) -> dict[str, Any]:
        settings = self._settings()
        return {
            "enabled": settings.general.schedule_enabled,
            "interval_min": settings.general.schedule_interval_min,
            "last_run": self._last_run.strftime("%H:%M") if self._last_run else None,
            "next_run": self._next_run.strftime("%H:%M") if (self._next_run and settings.general.schedule_enabled) else None,
            "last_result": self._last_result,
            "run_state": self.sup.state,
        }

    def reset(self) -> None:
        """После смены настроек таймер пересчитывается с нуля."""
        self._next_run = None
        self._last_result = None

    def schedule_next(self) -> None:
        """Сразу показать ближайший запуск, не дожидаясь тика цикла."""
        settings = self._settings()
        if settings.general.schedule_enabled:
            self._next_run = datetime.now() + timedelta(minutes=settings.general.schedule_interval_min)
        else:
            self._next_run = None

    # ---------- логика ----------

    async def tick(self, now: datetime | None = None) -> str:
        settings = self._settings()
        if not settings.general.schedule_enabled:
            self._next_run = None
            return "disabled"

        now = now or datetime.now()
        interval = settings.general.schedule_interval_min
        if self._next_run is None:
            self._next_run = now + timedelta(minutes=interval)
            return "scheduled"
        if now < self._next_run:
            return "waiting"

        if self.sup.state != "idle":
            # Не наслаиваем прогоны: сдвигаем окно, чтобы между ними была пауза.
            self._next_run = now + timedelta(minutes=interval)
            return "busy"

        if await self.db.count_pending() == 0:
            self._next_run = now + timedelta(minutes=interval)
            self._last_result = "очередь пуста"
            return "empty"

        result = await self.sup.start()
        self._last_run = now
        self._next_run = now + timedelta(minutes=interval)
        if result.get("ok"):
            self._last_result = f"запущен, потоков {result.get('threads')}"
            await self.bus.log(None, f"Планировщик: запуск прогона по расписанию ({self._last_result})")
            return "started"

        self._last_result = str(result.get("error"))[:200]
        await self.bus.log(None, f"Планировщик: запуск не удался — {self._last_result}", "warn")
        return "error"

    # ---------- цикл ----------

    def start_loop(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — планировщик не должен падать
                await self.bus.log(None, f"Планировщик: {type(exc).__name__}: {exc}", "error")
            await asyncio.sleep(15)
