"""Контракт таблицы и раскладка колонок листа «ПФ2»."""

from __future__ import annotations

from typing import Any, Protocol

# name -> 1-based индекс колонки в листе заданий.
TASK_COLUMNS: dict[str, int] = {
    "id": 1,
    "marketplace": 2,
    "sku": 3,
    "query": 4,
    "geo": 5,
    "target_runs": 6,
    "done_runs": 7,
    "start_position": 8,
    "position": 9,
    "cart_adds": 10,
    "likes": 11,
    "status": 12,
    "error": 13,
    "updated_at": 14,
}

TASK_HEADER = [
    "ID",
    "Маркетплейс",
    "Артикул / SKU",
    "Ключевой запрос",
    "Гео",
    "Требуется выполнений",
    "Выполнено",
    "Начальная позиция",
    "Фактическая позиция",
    "В корзину",
    "Лайков",
    "Статус",
    "Ошибка",
    "Обновлено",
]

ACCOUNTS_HEADER = [
    "ID",
    "Телефон",
    "Папка профиля",
    "Прокси",
    "Состояние",
    "Выполнено задач",
    "Последнее использование",
    "Комментарий",
]

ORDERS_HEADER = [
    "Дата",
    "ID задания",
    "SKU",
    "Аккаунт ID",
    "Телефон",
    "Папка профиля",
    "Прокси",
    "Позиция",
    "Статус выкупа",
    "Комментарий",
]

STATUS_TO_SHEET = {
    "pending": "Ожидает",
    "claimed": "В работе",
    "running": "В работе",
    "done": "Готово",
    "error": "Ошибка",
    "cancelled": "Отмена",
}

SHEET_TO_STATUS = {
    "ожидает": "pending",
    "в работе": "running",
    "готово": "done",
    "ошибка": "error",
    "отмена": "cancelled",
}

# Колонки, которые пишет только бот (G:N). Строка обновляется одним диапазоном.
BOT_RANGE_START = TASK_COLUMNS["done_runs"]
BOT_RANGE_END = TASK_COLUMNS["updated_at"]


class SheetBackend(Protocol):
    """Минимальный набор операций, который нужен синхронизации."""

    kind: str

    async def probe(self) -> dict[str, Any]: ...

    async def read(self, sheet: str) -> list[list[str]]: ...

    async def write_range(
        self, sheet: str, row: int, start_col: int, values: list[Any]
    ) -> None: ...

    async def append(self, sheet: str, values: list[Any]) -> None: ...


def col_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    text = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        text = chr(65 + rem) + text
    return text
