"""Excel-бэкенд: локальный .xlsx как таблица оператора.

Работает без Google и сети. Перед первой записью делает резервную копию,
сохраняет форматирование (насколько это умеет openpyxl) и не трогает
колонки, которых нет в раскладке.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import ORDERS_HEADER

SERVICE_SHEETS = {"Аккаунты", "Выкупы"}


class ExcelBackend:
    kind = "excel"

    def __init__(self, path: Path, sheet_tasks: str = "ПФ2") -> None:
        self.path = path
        self.sheet_tasks = sheet_tasks
        self._lock = asyncio.Lock()
        self._wb: Any = None
        self._mtime: float = 0.0
        self._backed_up = False

    # ---------- файл ----------

    def _open(self) -> Any:
        import openpyxl

        mtime = self.path.stat().st_mtime
        if self._wb is None or mtime != self._mtime:
            self._wb = openpyxl.load_workbook(self.path)
            self._mtime = mtime
        return self._wb

    def resolve_name(self, name: str) -> str:
        """Имя листа задач может не совпадать с настройкой.

        В файле пользователя лист называется «Таблица», а в настройках по
        умолчанию «ПФ2». Служебные листы при этом не должны становиться
        «листом заданий» — иначе бот начнёт писать задания в «Выкупы».
        """
        book = self._open()
        if name in book.sheetnames:
            return name
        if name == self.sheet_tasks:
            candidates = [title for title in book.sheetnames if title not in SERVICE_SHEETS]
            if len(candidates) == 1:
                return candidates[0]
        raise KeyError(f"лист «{name}» не найден: {', '.join(book.sheetnames)}")

    def _sheet(self, name: str) -> Any:
        book = self._open()
        return book[self.resolve_name(name)]

    def _backup(self) -> None:
        if self._backed_up:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.stem}.backup-{stamp}{self.path.suffix}")
        shutil.copy2(self.path, target)
        self._backed_up = True

    def _save(self) -> None:
        self._backup()
        try:
            self._wb.save(self.path)
        except PermissionError as exc:
            raise RuntimeError("Файл Excel открыт в другой программе — закройте его") from exc
        self._mtime = self.path.stat().st_mtime

    # ---------- контракт ----------

    async def probe(self) -> dict[str, Any]:
        try:
            async with self._lock:
                book = self._open()
                titles = list(book.sheetnames)
                try:
                    tasks_sheet = self.resolve_name(self.sheet_tasks)
                except KeyError:
                    tasks_sheet = None
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "mode": self.kind, "error": f"{type(exc).__name__}: {exc}"[:300]}

        return {
            "ok": tasks_sheet is not None,
            "mode": self.kind,
            "title": self.path.name,
            "sheets": titles,
            "tasks_sheet": tasks_sheet,
            "missing": [] if tasks_sheet else [self.sheet_tasks],
            "path": str(self.path),
        }

    async def read(self, sheet: str) -> list[list[str]]:
        async with self._lock:
            ws = self._sheet(sheet)
            rows = [
                ["" if value is None else str(value) for value in row]
                for row in ws.iter_rows(values_only=True)
            ]
            while rows and not any(cell.strip() for cell in rows[-1]):
                rows.pop()
            return rows

    async def write_range(self, sheet: str, row: int, start_col: int, values: list[Any]) -> None:
        async with self._lock:
            ws = self._sheet(sheet)
            for offset, value in enumerate(values):
                ws.cell(row=row, column=start_col + offset, value=value)
            self._save()

    async def append(self, sheet: str, values: list[Any]) -> None:
        async with self._lock:
            book = self._open()
            if sheet not in book.sheetnames:
                raise KeyError(f"лист «{sheet}» не найден: {', '.join(book.sheetnames)}")
            book[sheet].append(list(values))
            self._save()

    async def ensure_sheet(self, name: str, header: list[Any]) -> None:
        """Создаёт служебный лист только когда он реально нужен (выкуп)."""
        async with self._lock:
            book = self._open()
            if name in book.sheetnames:
                return
            ws = book.create_sheet(name)
            ws.append(header)
            self._save()
