"""Двусторонняя синхронизация: SQLite — истина по очереди, таблица — панель оператора.

Работает с тремя бэкендами (Google / Excel / локальный JSON) и сам определяет
раскладку колонок по заголовкам: чужая таблица с «Кол заданий» и «Позиция Факт»
подхватывается без правки кода.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import AppSettings, load_settings
from ..core.events import EventBus
from ..db.database import Database
from .base import SHEET_TO_STATUS, STATUS_TO_SHEET, SheetBackend
from .excel_backend import ExcelBackend
from .fake import FakeSheetBackend
from .gspread_backend import GspreadBackend
from .layout import detect_layout, extract_sku


def build_backend(settings: AppSettings) -> SheetBackend:
    general = settings.general

    if (
        general.spreadsheet_id
        and general.google_credentials
        and Path(general.google_credentials).exists()
    ):
        return GspreadBackend(
            general.google_credentials,
            general.spreadsheet_id,
            [general.sheet_tasks, general.sheet_accounts, general.sheet_orders],
        )
    if general.sheet_xlsx and Path(general.sheet_xlsx).exists():
        return ExcelBackend(Path(general.sheet_xlsx), general.sheet_tasks)
    return FakeSheetBackend()


def _groups(pairs: list[tuple[int, Any]]) -> list[tuple[int, list[Any]]]:
    """Склеивает соседние колонки в один диапазон, чтобы не писать по одной ячейке."""
    groups: list[tuple[int, list[Any]]] = []
    for column, value in pairs:
        if groups and column == groups[-1][0] + len(groups[-1][1]):
            groups[-1][1].append(value)
        else:
            groups.append((column, [value]))
    return groups


class SyncEngine:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._layout_cache: dict[str, Any] | None = None
        self.state: dict[str, Any] = {
            "backend": "fake",
            "mode": "тестовая таблица",
            "enabled": True,
            "last_pull": None,
            "last_push": None,
            "pulled": 0,
            "pushed": 0,
            "last_error": None,
            "interval": 30,
        }

    # ---------- публичное ----------

    async def probe(self) -> dict[str, Any]:
        settings = load_settings()
        general = settings.general
        if general.sheet_xlsx and not Path(general.sheet_xlsx).exists():
            self._set_backend(FakeSheetBackend())
            self.state["mode"] = "Excel: файл не найден"
            return {
                "ok": False,
                "mode": "excel",
                "error": f"Файл не найден: {general.sheet_xlsx}",
            }
        backend = build_backend(settings)
        self._set_backend(backend)
        result = await backend.probe()
        if result.get("ok"):
            self.state["last_error"] = None
        await self.bus.publish("sync_state", self.state)
        return result

    def status(self) -> dict[str, Any]:
        """Лёгкое состояние для UI: без чтения файла и сетевых вызовов."""
        settings = load_settings()
        general = settings.general
        if general.sheet_xlsx and not Path(general.sheet_xlsx).exists():
            self.state["backend"] = "excel"
            self.state["mode"] = "Excel: файл не найден"
            self.state["last_error"] = f"Файл не найден: {general.sheet_xlsx}"
        else:
            self._set_backend(build_backend(settings))
            if not (general.spreadsheet_id or general.sheet_xlsx):
                self.state["mode"] = "тестовая таблица (не настроена)"
            # Ошибка «файл не найден» относилась к старому пути: после выбора
            # существующего файла она не должна висеть в панели.
            error = str(self.state.get("last_error") or "")
            if "Файл не найден" in error:
                self.state["last_error"] = None
        self.state["enabled"] = general.sync_enabled
        self.state["interval"] = general.sync_interval_sec
        return dict(self.state)

    async def cycle(self) -> dict[str, Any]:
        if self._lock.locked():
            return {"ok": False, "error": "Синхронизация уже идёт", **self.state}

        async with self._lock:
            settings = load_settings()
            general = settings.general
            if general.sheet_xlsx and not Path(general.sheet_xlsx).exists():
                message = f"Файл не найден: {general.sheet_xlsx}"
                self.state.update(
                    {"backend": "excel", "mode": "Excel: файл не найден", "last_error": message}
                )
                await self.bus.publish("sync_state", self.state)
                return {"ok": False, "error": message, **self.state}
            backend = build_backend(settings)
            self._set_backend(backend)
            pulled = pushed = 0
            try:
                await self._prepare(backend)
                source = self._source_id(backend, settings)
                await self._switch_source(source)
                pulled, wrote_ids = await self._pull(backend, settings, source)
                pushed = await self._push(backend, settings, source)
                self.state.update(
                    {
                        "pulled": pulled,
                        "pushed": pushed,
                        "last_pull": datetime.now().strftime("%H:%M:%S"),
                        "last_push": datetime.now().strftime("%H:%M:%S"),
                        "last_error": None,
                    }
                )
                if wrote_ids:
                    await self.bus.log(None, f"Таблица: выдано ID новым строкам — {wrote_ids}")
            except Exception as exc:  # noqa: BLE001 — синк не должен ронять прогон
                self.state["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                await self.bus.log(None, f"Ошибка синхронизации: {exc}", "warn")
                await self.bus.publish("sync_state", self.state)
                return {"ok": False, "error": self.state["last_error"], **self.state}

            await self.bus.publish("sync_state", self.state)
            return {"ok": True, **self.state}

    async def push_now(self) -> int:
        if self._lock.locked():
            return 0
        async with self._lock:
            settings = load_settings()
            general = settings.general
            if general.sheet_xlsx and not Path(general.sheet_xlsx).exists():
                self.state["last_error"] = f"Файл не найден: {general.sheet_xlsx}"
                return 0
            backend = build_backend(settings)
            try:
                pushed = await self._push(backend, settings, self._source_id(backend, settings))
            except Exception as exc:  # noqa: BLE001
                self.state["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                await self.bus.log(None, f"Не удалось обновить таблицу: {exc}", "warn")
                return 0
            self.state.update({"pushed": pushed, "last_push": datetime.now().strftime("%H:%M:%S")})
            await self.bus.publish("sync_state", self.state)
            return pushed

    async def append_order(self, task: dict[str, Any], position: int | None) -> None:
        settings = load_settings()
        if settings.general.sheet_xlsx and not Path(settings.general.sheet_xlsx).exists():
            await self.bus.log(None, "Выкуп не записан: файл Excel не найден", "warn")
            return
        backend = build_backend(settings)
        values = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            task.get("sheet_id") or f"local-{task['id']}",
            task["sku"],
            "",
            "",
            "",
            "",
            position or "",
            "Ждёт",
            "добавлено ботом",
        ]
        try:
            if backend.kind == "excel":
                from .base import ORDERS_HEADER

                await backend.ensure_sheet(settings.general.sheet_orders, ORDERS_HEADER)  # type: ignore[attr-defined]
            await backend.append(settings.general.sheet_orders, values)
        except Exception as exc:  # noqa: BLE001
            await self.bus.log(None, f"Не удалось записать выкуп: {exc}", "warn")

    # ---------- цикл ----------

    def start_loop(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sheet-sync")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(1)
        while not self._stopping:
            settings = load_settings()
            if settings.general.sync_enabled:
                await self.cycle()
                interval = max(10, settings.general.sync_interval_sec)
            else:
                interval = 10
            await asyncio.sleep(interval)

    # ---------- служебное ----------

    def _set_backend(self, backend: SheetBackend) -> None:
        self.state["backend"] = backend.kind
        self.state["mode"] = {
            "google": "Google Sheets",
            "excel": "Excel (.xlsx)",
        }.get(backend.kind, "тестовая таблица")

    def _source_id(self, backend: SheetBackend, settings: AppSettings) -> str:
        general = settings.general
        if backend.kind == "excel":
            return f"excel:{Path(general.sheet_xlsx).resolve()}".lower()
        if backend.kind == "google":
            return f"google:{general.spreadsheet_id}"
        return "fake"

    async def _switch_source(self, source: str) -> None:
        """При смене таблицы старые задания не должны попасть в новый лист."""
        row = await self.db.fetchone("SELECT value FROM settings WHERE key = 'sync_source'")
        previous = row["value"] if row else None
        if previous == source:
            return

        if previous:
            # Не тронутые демо-задания удаляем, остальные — на паузу.
            if previous == "fake":
                await self.db.execute(
                    "DELETE FROM tasks WHERE source = 'fake'"
                    " AND status = 'pending' AND done_runs = 0"
                )
            await self.db.execute(
                "UPDATE tasks SET status = 'paused'"
                " WHERE source = ? AND status IN ('pending', 'claimed', 'running')",
                (previous,),
            )
        await self.db.execute(
            "UPDATE tasks SET status = 'pending'"
            " WHERE source = ? AND status = 'paused' AND done_runs < target_runs",
            (source,),
        )
        await self.db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('sync_source', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE
               SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (source,),
        )
        suffix = f", задания «{previous}» приостановлены" if previous else ""
        await self.bus.log(None, f"Источник таблицы: {source}{suffix}")

    async def _prepare(self, backend: SheetBackend) -> None:
        """Ничего не создаём заранее: служебные листы появляются только при выкупе."""
        return None

    def _layout(self, backend: SheetBackend, sheet: str, header: list[Any]) -> dict[str, int]:
        key = (backend.kind, sheet, tuple(str(cell) for cell in header))
        cache = self._layout_cache
        if cache and cache["key"] == key and time.monotonic() - cache["ts"] < 300:
            return cache["layout"]
        layout = detect_layout(header)
        self._layout_cache = {"key": key, "layout": layout, "ts": time.monotonic()}
        return layout

    async def _layout_for(self, backend: SheetBackend, settings: AppSettings) -> dict[str, int]:
        sheet = settings.general.sheet_tasks
        rows = await backend.read(sheet)
        header = rows[0] if rows else []
        return self._layout(backend, sheet, header)

    # ---------- шаги ----------

    async def _pull(
        self, backend: SheetBackend, settings: AppSettings, source: str | None = None
    ) -> tuple[int, int]:
        source = source or self._source_id(backend, settings)
        sheet = settings.general.sheet_tasks
        rows = await backend.read(sheet)
        if not rows:
            return 0, 0

        layout = self._layout(backend, sheet, rows[0])

        def cell(row: list[str], key: str) -> str:
            index = layout.get(key)
            if not index or index > len(row):
                return ""
            return str(row[index - 1]).strip()

        pulled = 0
        wrote_ids = 0
        for sheet_row, row in enumerate(rows[1:], start=2):
            sku_raw = cell(row, "sku")
            query = cell(row, "query")
            if not sku_raw and not query:
                continue

            sku, url = extract_sku(sku_raw)
            sheet_id = cell(row, "id")
            if not sheet_id:
                if "id" in layout:
                    sheet_id = str(uuid.uuid4())
                    await backend.write_range(sheet, sheet_row, layout["id"], [sheet_id])
                    wrote_ids += 1
                else:
                    # В чужой таблице нет колонки ID: ключ стабилен, пока не двигали строки.
                    sheet_id = f"{backend.kind}:{sheet}:{sheet_row}"

            target_raw = cell(row, "target_runs") or "1"
            try:
                target = max(1, int(float(target_raw.replace(",", "."))))
            except ValueError:
                target = 1

            status_cell = cell(row, "status")
            if not status_cell:
                status_cell = cell(row, "status_total")
            op_status = SHEET_TO_STATUS.get(status_cell.lower())
            existing = await self.db.fetchone("SELECT * FROM tasks WHERE sheet_id = ?", (sheet_id,))

            if existing is None:
                # Строка из готового листа не должна запускаться заново после сброса БД
                # и не должна затирать счётчики нулями.
                status = "cancelled" if op_status == "cancelled" else "pending"
                done_runs = 0
                if op_status == "done":
                    status = "done"
                    done_runs = target
                start_position = _int(cell(row, "start_position"))
                position = _int(cell(row, "position"))
                cart_adds = _int(cell(row, "cart_adds")) or 0
                likes = _int(cell(row, "likes")) or 0
                await self.db.execute(
                    """
                    INSERT INTO tasks (sheet_id, sheet_row, marketplace, sku, url, query, geo,
                                       target_runs, status, source, done_runs,
                                       start_position, position, cart_adds, likes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        sheet_id,
                        sheet_row,
                        cell(row, "marketplace") or "ozon",
                        sku,
                        url,
                        query,
                        cell(row, "geo"),
                        target,
                        status,
                        source,
                        done_runs,
                        start_position,
                        position,
                        cart_adds,
                        likes,
                    ),
                )
                pulled += 1
                continue

            status = existing["status"]
            last_error_text = existing["last_error"]
            sku_changed = bool(sku and sku != (existing["sku"] or ""))
            if sku_changed:
                # В строке теперь другой товар: прежние статус, прогресс и
                # ошибка к нему не относятся. Иначе «снятый» свитер блокирует
                # новую сковороду и тащит в неё чужие позиции/корзины.
                status = "pending"
                last_error_text = None
                done_runs = 0
                start_position = None
                position = None
                cart_adds = 0
                likes = 0
            else:
                done_runs = existing["done_runs"]
                start_position = existing["start_position"]
                position = existing["position"]
                cart_adds = existing["cart_adds"]
                likes = existing["likes"]
            last_error = (last_error_text or "").lower()
            # Товар снят/закончился: синк не должен снова ставить строку в очередь.
            terminal = "недоступен" in last_error or "закончился" in last_error
            if op_status == "cancelled":
                status = "cancelled"
            elif (
                not sku_changed
                and op_status in (None, "pending")
                and status in ("cancelled", "error", "done")
            ):
                if existing["done_runs"] < target and not terminal:
                    status = "pending"

            fields = {
                "sheet_row": sheet_row,
                "marketplace": cell(row, "marketplace") or existing["marketplace"],
                "sku": sku or existing["sku"],
                "url": url or existing["url"],
                "query": query or existing["query"],
                "geo": cell(row, "geo") or existing["geo"],
                "target_runs": target,
                "status": status,
                "source": source,
                "last_error": last_error_text,
                "done_runs": done_runs,
                "start_position": start_position,
                "position": position,
                "cart_adds": cart_adds,
                "likes": likes,
            }
            if all(existing[key] == value for key, value in fields.items()):
                continue

            await self.db.execute(
                """
                UPDATE tasks
                   SET sheet_row = ?, marketplace = ?, sku = ?, url = ?, query = ?, geo = ?,
                       target_runs = ?, status = ?, source = ?, last_error = ?,
                       done_runs = ?, start_position = ?, position = ?, cart_adds = ?, likes = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    fields["sheet_row"],
                    fields["marketplace"],
                    fields["sku"],
                    fields["url"],
                    fields["query"],
                    fields["geo"],
                    fields["target_runs"],
                    fields["status"],
                    fields["source"],
                    fields["last_error"],
                    fields["done_runs"],
                    fields["start_position"],
                    fields["position"],
                    fields["cart_adds"],
                    fields["likes"],
                    existing["id"],
                ),
            )
            pulled += 1

        return pulled, wrote_ids

    async def _push(
        self, backend: SheetBackend, settings: AppSettings, source: str | None = None
    ) -> int:
        source = source or self._source_id(backend, settings)
        sheet = settings.general.sheet_tasks
        layout = await self._layout_for(backend, settings)
        tasks = await self.db.fetchall(
            "SELECT * FROM tasks WHERE sheet_row IS NOT NULL AND sheet_id IS NOT NULL"
            " AND source = ? ORDER BY sheet_row",
            (source,),
        )
        pushed = 0
        for task in tasks:
            pairs: dict[int, Any] = {}

            def put(key: str, value: Any) -> None:
                column = layout.get(key)
                if column:
                    pairs[column] = value

            status_text = STATUS_TO_SHEET.get(task["status"], task["status"])
            put("done_runs", task["done_runs"])
            put("start_position", task["start_position"] if task["start_position"] is not None else "")
            put("position", task["position"] if task["position"] is not None else "")
            put("cart_adds", task["cart_adds"])
            put("likes", task["likes"])
            put("status", status_text)
            put("error", (task["last_error"] or "")[:300])
            put("updated_at", _local_ts(task["updated_at"]))
            if task["status"] in ("done", "error", "cancelled"):
                put("status_total", status_text)

            ordered = sorted(pairs.items())
            # Источник входит в отпечаток: при смене Excel тот же task_id
            # должен выгрузиться в новый файл, а не считаться уже записанным.
            payload = json.dumps(
                {"source": source, "cells": ordered}, ensure_ascii=False
            )
            previous = await self.db.fetchone(
                "SELECT payload FROM sheet_state WHERE task_id = ?", (task["id"],)
            )
            if previous and previous["payload"] == payload:
                continue

            for start_col, values in _groups(ordered):
                await backend.write_range(sheet, int(task["sheet_row"]), start_col, values)

            await self.db.execute(
                """
                INSERT INTO sheet_state (task_id, payload, pushed_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(task_id) DO UPDATE
                   SET payload = excluded.payload, pushed_at = excluded.pushed_at
                """,
                (task["id"], payload),
            )
            pushed += 1
        return pushed


def _int(value: Any) -> int | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _local_ts(value: str | None) -> str:
    """SQLite хранит UTC — в таблицу отдаём локальное время."""
    if not value:
        return ""
    try:
        stamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
