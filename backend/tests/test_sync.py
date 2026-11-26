"""Проверка двусторонней синхронизации на тестовой таблице.

python tests/test_sync.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import AppSettings  # noqa: E402
from app.core.events import EventBus  # noqa: E402
from app.db.database import Database  # noqa: E402
from app.sheets.fake import FakeSheetBackend  # noqa: E402
from app.sheets.sync import SyncEngine  # noqa: E402


class CountingFake(FakeSheetBackend):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.writes = 0

    async def write_range(self, sheet, row, start_col, values):  # type: ignore[no-untyped-def]
        self.writes += 1
        await super().write_range(sheet, row, start_col, values)


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pf-sync-"))
    db = Database(tmp / "test.db")
    await db.connect()
    try:
        await run_assertions(db, tmp)
    finally:
        await db.close()
    print("sync: ok — идемпотентный pull, ID в лист, push только изменений, статус оператора")


async def run_assertions(db: Database, tmp: Path) -> None:
    backend = CountingFake(tmp / "sheet.json")
    engine = SyncEngine(db, EventBus())
    settings = AppSettings()

    pulled, wrote_ids = await engine._pull(backend, settings)
    assert pulled == 3, f"импортировано {pulled}"
    assert wrote_ids == 0, "в тестовых данных ID уже есть"

    rows = (await backend.read(settings.general.sheet_tasks))[1:]
    assert all(row[0] for row in rows), "у всех строк должен быть ID"

    tasks = await db.fetchall("SELECT * FROM tasks ORDER BY id")
    assert len(tasks) == 3, f"в БД {len(tasks)} заданий"

    pulled_again, _ = await engine._pull(backend, settings)
    assert pulled_again == 0, "повторный pull не должен ничего менять"
    assert len(await db.fetchall("SELECT id FROM tasks")) == 3, "появились дубли"

    # Первый push отправляет исходное состояние всех строк.
    backend.writes = 0
    assert await engine._push(backend, settings) == 3
    assert backend.writes == 3

    # Результат прогона уходит в лист ровно один раз.
    target = tasks[0]
    await db.execute(
        "UPDATE tasks SET done_runs = 1, position = 42, likes = 1, status = 'done',"
        " updated_at = '2026-01-01 10:00:00' WHERE id = ?",
        (target["id"],),
    )
    backend.writes = 0
    pushed = await engine._push(backend, settings)
    assert pushed == 1 and backend.writes == 1, f"push={pushed}, записей={backend.writes}"
    sheet_row = (await backend.read(settings.general.sheet_tasks))[1 + target["sheet_row"] - 2]
    assert sheet_row[6] == "1" and sheet_row[8] == "42", sheet_row
    assert sheet_row[11] == "Готово", sheet_row

    backend.writes = 0
    assert await engine._push(backend, settings) == 0, "без изменений писать нечего"
    assert backend.writes == 0

    # Оператор отменяет строку — задание уходит в cancelled.
    await backend.write_range(settings.general.sheet_tasks, target["sheet_row"], 12, ["Отмена"])
    await engine._pull(backend, settings)
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (target["id"],))
    assert row["status"] == "cancelled", row

    # Оператор вернул «Ожидает» — задание снова в очереди.
    await backend.write_range(settings.general.sheet_tasks, target["sheet_row"], 12, ["Ожидает"])
    await db.execute("UPDATE tasks SET target_runs = 5, done_runs = 1 WHERE id = ?", (target["id"],))
    await engine._pull(backend, settings)
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (target["id"],))
    assert row["status"] == "pending", row


if __name__ == "__main__":
    asyncio.run(scenario())
