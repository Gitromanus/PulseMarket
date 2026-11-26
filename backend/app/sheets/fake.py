"""Фейковый бэкенд таблицы: тот же контракт, но данные лежат в JSON.

Нужен, чтобы полностью проверить цикл синхронизации без сервисного аккаунта
Google и без сети. Формат повторяет листы: header + rows (список списков).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .base import ACCOUNTS_HEADER, ORDERS_HEADER, TASK_HEADER


def _default_book() -> dict[str, Any]:
    return {
        "title": "PulseMarket — тестовая таблица",
        "sheets": {
            "ПФ2": {
                "header": TASK_HEADER,
                "rows": [
                    [
                        str(uuid.uuid4()),
                        "ozon",
                        "SHEET-2001",
                        "беспроводная зарядка",
                        "https://www.ozon.ru/city/moskva/",
                        "2", "0", "", "", "0", "0", "Ожидает", "", "",
                    ],
                    [
                        str(uuid.uuid4()),
                        "ozon",
                        "SHEET-2002",
                        "термокружка 500 мл",
                        "https://www.ozon.ru/city/sankt-peterburg/",
                        "3", "0", "", "", "0", "0", "Ожидает", "", "",
                    ],
                    [
                        str(uuid.uuid4()),
                        "ozon",
                        "SHEET-2003",
                        "коврик для йоги",
                        "https://www.ozon.ru/city/kazan/",
                        "2", "0", "", "", "0", "0", "Ожидает", "", "",
                    ],
                ],
            },
            "Аккаунты": {
                "header": ACCOUNTS_HEADER,
                "rows": [
                    ["ACC-1", "+79000000001", r"C:\profiles\active\acc1", "socks5://user:pass@127.0.0.1:1080", "Свободен", "0", "", "демо"],
                    ["ACC-2", "+79000000002", r"C:\profiles\active\acc2", "socks5://user:pass@127.0.0.1:1081", "Свободен", "0", "", "демо"],
                ],
            },
            "Выкупы": {"header": ORDERS_HEADER, "rows": []},
        },
    }


class FakeSheetBackend:
    kind = "fake"

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            base = os.getenv("PF_APP_DATA") or os.getenv("APPDATA") or str(Path.home())
            path = Path(base) / "pf-automation" / "fake_sheet.json"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    # ---------- файл ----------

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            book = _default_book()
            self.path.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
            return book
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Тестовые данные можно потерять, но приложение не должно падать.
            book = _default_book()
            self.path.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
            return book

    def _save(self, book: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _sheet(self, book: dict[str, Any], name: str) -> dict[str, Any]:
        if name not in book["sheets"]:
            book["sheets"][name] = {"header": TASK_HEADER, "rows": []}
        return book["sheets"][name]

    # ---------- контракт ----------

    async def probe(self) -> dict[str, Any]:
        book = self._load()
        return {
            "ok": True,
            "mode": self.kind,
            "title": book["title"],
            "sheets": list(book["sheets"].keys()),
            "path": str(self.path),
            "missing": [],
        }

    async def read(self, sheet: str) -> list[list[str]]:
        async with self._lock:
            book = self._load()
            data = self._sheet(book, sheet)
            return [list(data["header"])] + [[str(c) for c in row] for row in data["rows"]]

    async def write_range(self, sheet: str, row: int, start_col: int, values: list[Any]) -> None:
        if row < 1:
            raise ValueError("row должен быть >= 1")
        async with self._lock:
            book = self._load()
            data = self._sheet(book, sheet)
            index = row - 2  # строка 1 — заголовок
            while len(data["rows"]) <= index:
                data["rows"].append([])
            target = data["rows"][index]
            while len(target) < start_col - 1 + len(values):
                target.append("")
            for offset, value in enumerate(values):
                target[start_col - 1 + offset] = "" if value is None else str(value)
            self._save(book)
        await asyncio.sleep(0)

    async def append(self, sheet: str, values: list[Any]) -> None:
        async with self._lock:
            book = self._load()
            data = self._sheet(book, sheet)
            data["rows"].append(["" if v is None else str(v) for v in values])
            self._save(book)
        await asyncio.sleep(0)
