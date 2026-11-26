"""Excel-режим: чтение чужой таблицы, запись результатов, бэкап.

python tests/test_excel.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402

from app.core.config import AppSettings  # noqa: E402
from app.core.events import EventBus  # noqa: E402
from app.db.database import Database  # noqa: E402
from app.sheets.excel_backend import ExcelBackend  # noqa: E402
from app.sheets.sync import SyncEngine  # noqa: E402

HEADER = [
    "Дата",
    "SKU",
    "Ключевой запрос",
    "Использовать фильтр",
    "Необходимое кол заходов",
    "Город",
    "Кол заданий",
    "Позиция Начальная",
    "Позиция Факт",
    "Кол добавлений в корзину",
    "Кол лайков",
    "Статус итог",
    "Статус",
]

ROWS = [
    [
        "01.01.2026",
        "https://www.ozon.ru/product/lampa-svetodiodnaya-gu10-3940020239/?at=1k&sh=abc",
        "лампочка",
        "",
        "",
        "",
        5,
        "",
        "",
        "",
        "",
        "",
        "",
    ],
    [
        "",
        "https://www.ozon.ru/product/sviter-happyfox-1134432544/?at=1k&sh=abc",
        "свитер женский",
        "",
        "",
        "",
        10,
        "",
        "",
        "",
        "",
        "",
        "",
    ],
]


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pf-excel-"))
    path = tmp / "sheet.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Таблица"
    sheet.append(HEADER)
    for row in ROWS:
        sheet.append(row)
    book.save(path)

    db = Database(tmp / "test.db")
    await db.connect()
    try:
        settings = AppSettings()
        settings.general.sheet_xlsx = str(path)
        settings.general.sheet_tasks = "ПФ2"  # в файле лист называется иначе
        backend = ExcelBackend(path, settings.general.sheet_tasks)
        engine = SyncEngine(db, EventBus())

        probe = await backend.probe()
        assert probe["ok"] and probe["mode"] == "excel", probe
        assert probe["sheets"] == ["Таблица"], probe

        # Задание из другого источника не должно попасть в этот файл.
        await db.execute(
            """
            INSERT INTO tasks (sheet_id, sheet_row, marketplace, sku, query, target_runs, status, source)
            VALUES ('fake:1', 2, 'ozon', 'FAKE-SKU', 'чужое', 99, 'pending', 'fake')
            """
        )
        await db.execute(
            """
            INSERT INTO tasks (sheet_id, sheet_row, marketplace, sku, query, target_runs,
                               status, source, done_runs)
            VALUES ('fake:2', 3, 'ozon', 'FAKE-DONE', 'чужое', 99, 'pending', 'fake', 2)
            """
        )
        await db.execute("INSERT INTO settings (key, value) VALUES ('sync_source', 'fake')")

        source = engine._source_id(backend, settings)
        await engine._switch_source(source)
        untouched = await db.fetchone("SELECT id FROM tasks WHERE sku = 'FAKE-SKU'")
        assert untouched is None, "нетронутое демо-задание должно удаляться"
        touched = await db.fetchone("SELECT status FROM tasks WHERE sku = 'FAKE-DONE'")
        assert touched["status"] == "paused", touched

        pulled, wrote_ids = await engine._pull(backend, settings, source)
        assert pulled == 2 and wrote_ids == 0, (pulled, wrote_ids)

        tasks = await db.fetchall("SELECT * FROM tasks WHERE source = ? ORDER BY id", (source,))
        assert len(tasks) == 2, tasks
        assert tasks[0]["sku"] == "3940020239", tasks[0]
        assert tasks[0]["url"].startswith("https://www.ozon.ru/product/"), tasks[0]
        assert tasks[0]["target_runs"] == 5 and tasks[1]["target_runs"] == 10
        assert tasks[0]["query"] == "лампочка"

        # Исходная запись уходит в лист, ID и дата не трогаются.
        assert await engine._push(backend, settings) == 2
        check = openpyxl.load_workbook(path)["Таблица"]
        assert check.cell(2, 1).value == "01.01.2026", "дату перезаписали"
        assert check.cell(2, 13).value == "Ожидает"
        assert check.cell(2, 12).value in (None, "")

        # Результат прогона: позиции, счётчики, статус.
        await db.execute(
            "UPDATE tasks SET done_runs = 1, start_position = 17, position = 9,"
            " cart_adds = 1, likes = 1, status = 'done', updated_at = '2026-01-01 10:00:00'"
            " WHERE id = ?",
            (tasks[0]["id"],),
        )
        assert await engine._push(backend, settings) == 1
        check = openpyxl.load_workbook(path)["Таблица"]
        assert check.cell(2, 8).value == 17, check.cell(2, 8).value
        assert check.cell(2, 9).value == 9
        assert check.cell(2, 10).value == 1
        assert check.cell(2, 11).value == 1
        assert check.cell(2, 13).value == "Готово"
        assert check.cell(2, 12).value == "Готово"

        # Повторный push без изменений ничего не пишет.
        assert await engine._push(backend, settings) == 0

        # Служебные листы не создаются заранее и не подменяют лист заданий.
        assert openpyxl.load_workbook(path).sheetnames == ["Таблица"]
        await backend.ensure_sheet("Выкупы", ["Дата", "ID задания", "SKU"])
        assert set(openpyxl.load_workbook(path).sheetnames) >= {"Таблица", "Выкупы"}
        assert await backend.read("ПФ2"), "лист заданий перестал определяться"

        # Оператор отменяет строку в Excel — задание снимается.
        await backend.write_range("ПФ2", 3, 13, ["Отмена"])
        await engine._pull(backend, settings)
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (tasks[1]["id"],))
        assert row["status"] == "cancelled", row

        backups = list(tmp.glob("sheet.backup-*.xlsx"))
        assert backups, "резервная копия не создана"

        # Сброс БД не должен заново запускать готовые строки и затирать счётчики.
        await db.execute("DELETE FROM sheet_state")
        await db.execute("DELETE FROM tasks")
        pulled, _ = await engine._pull(backend, settings)
        assert pulled == 2, pulled
        db_row = await db.fetchone(
            "SELECT * FROM tasks WHERE sheet_id = 'excel:ПФ2:2'"
        )
        assert db_row["status"] == "done", db_row
        assert db_row["done_runs"] == db_row["target_runs"] == 5, db_row
        assert db_row["position"] == 9 and db_row["start_position"] == 17, db_row
        assert db_row["cart_adds"] == 1 and db_row["likes"] == 1, db_row
    finally:
        await db.close()

    print("excel: ok — импорт по заголовкам, запись без порчи даты, статусы, бэкап")


if __name__ == "__main__":
    asyncio.run(scenario())
