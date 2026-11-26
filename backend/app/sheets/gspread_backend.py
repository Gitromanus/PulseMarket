"""Google Sheets через gspread. Все блокирующие вызовы уходят в поток."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .base import col_letter

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class GspreadBackend:
    kind = "google"

    def __init__(self, credentials: str, spreadsheet_id: str, required_sheets: list[str]) -> None:
        self.credentials = credentials
        self.spreadsheet_id = spreadsheet_id
        self.required_sheets = required_sheets
        self._book: Any = None
        self._lock = asyncio.Lock()
        self._last_write = 0.0

    # ---------- внутреннее ----------

    def _open_sync(self) -> Any:
        if self._book is None:
            import gspread
            from google.oauth2.service_account import Credentials

            creds = Credentials.from_service_account_file(self.credentials, scopes=SCOPES)
            self._book = gspread.authorize(creds).open_by_key(self.spreadsheet_id)
        return self._book

    def _worksheet_sync(self, sheet: str) -> Any:
        return self._open_sync().worksheet(sheet)

    async def probe(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            book = self._open_sync()
            titles = [ws.title for ws in book.worksheets()]
            missing = [name for name in self.required_sheets if name not in titles]
            return {
                "ok": not missing,
                "mode": self.kind,
                "title": book.title,
                "sheets": titles,
                "missing": missing,
            }

        try:
            async with self._lock:
                return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "mode": self.kind, "error": f"{type(exc).__name__}: {exc}"[:400]}

    async def read(self, sheet: str) -> list[list[str]]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self._worksheet_sync(sheet).get_all_values()
            )

    async def write_range(self, sheet: str, row: int, start_col: int, values: list[Any]) -> None:
        def run() -> None:
            ws = self._worksheet_sync(sheet)
            end_col = start_col + len(values) - 1
            a1 = f"{col_letter(start_col)}{row}:{col_letter(end_col)}{row}"
            ws.update(range_name=a1, values=[[("" if v is None else v) for v in values]])

        async with self._lock:
            # Держим ~55 записей в минуту: лимит Google — 60 на пользователя.
            wait = 1.05 - (time.monotonic() - self._last_write)
            if wait > 0:
                await asyncio.sleep(wait)
            await asyncio.to_thread(run)
            self._last_write = time.monotonic()

    async def append(self, sheet: str, values: list[Any]) -> None:
        def run() -> None:
            ws = self._worksheet_sync(sheet)
            ws.append_row(["" if v is None else v for v in values], value_input_option="USER_ENTERED")

        async with self._lock:
            wait = 1.05 - (time.monotonic() - self._last_write)
            if wait > 0:
                await asyncio.sleep(wait)
            await asyncio.to_thread(run)
            self._last_write = time.monotonic()
