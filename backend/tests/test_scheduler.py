"""Планировщик (M5): логика запуска по интервалу без реального ожидания.

python tests/test_scheduler.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import AppSettings  # noqa: E402
from app.core.events import EventBus  # noqa: E402
from app.core.scheduler import Scheduler  # noqa: E402


class FakeSup:
    def __init__(self, state: str = "idle", ok: bool = True) -> None:
        self.state = state
        self.ok = ok
        self.started = 0

    async def start(self) -> dict:
        self.started += 1
        if self.ok:
            self.state = "running"
            return {"ok": True, "threads": 2}
        return {"ok": False, "error": "нет заданий"}


class FakeDb:
    def __init__(self, pending: int) -> None:
        self.pending = pending

    async def count_pending(self) -> int:
        return self.pending


def make_settings(enabled: bool, interval: int = 10) -> AppSettings:
    settings = AppSettings()
    settings.general.schedule_enabled = enabled
    settings.general.schedule_interval_min = interval
    return settings


async def scenario() -> None:
    bus = EventBus()
    start = datetime(2026, 1, 1, 10, 0, 0)

    # Выключен: ничего не делает.
    sup = FakeSup()
    sched = Scheduler(sup, FakeDb(5), bus, lambda: make_settings(False))  # type: ignore[arg-type]
    assert await sched.tick(start) == "disabled"
    assert sup.started == 0

    # Включён: первый тик планирует, второй до срока ждёт.
    settings = make_settings(True, 10)
    sup = FakeSup()
    db = FakeDb(5)
    sched = Scheduler(sup, db, bus, lambda: settings)  # type: ignore[arg-type]
    assert await sched.tick(start) == "scheduled"
    state = sched.state()
    assert state["next_run"] == "10:10", state
    assert await sched.tick(start + timedelta(minutes=5)) == "waiting"
    assert sup.started == 0

    # Срок пришёл — запуск.
    assert await sched.tick(start + timedelta(minutes=11)) == "started"
    assert sup.started == 1
    assert sched.state()["next_run"] == "10:21"

    # Прогон идёт — не мешаем и сдвигаем окно на следующий интервал.
    assert await sched.tick(start + timedelta(minutes=25)) == "busy"
    assert sup.started == 1
    assert sched.state()["next_run"] == "10:35"

    # Очередь пуста — переносим следующий запуск без старта.
    sup.state = "idle"
    db.pending = 0
    assert await sched.tick(start + timedelta(minutes=36)) == "empty"
    assert sup.started == 1
    assert sched.state()["next_run"] == "10:46"

    # Ошибка старта фиксируется, а не теряется.
    db.pending = 2
    sup.ok = False
    assert await sched.tick(start + timedelta(minutes=47)) == "error"
    assert "нет заданий" in (sched.state()["last_result"] or "")

    print("scheduler: ok — disabled, планирование, ожидание, запуск, busy, пустая очередь, ошибка")


if __name__ == "__main__":
    asyncio.run(scenario())
