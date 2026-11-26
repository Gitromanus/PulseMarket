"""Определение колонок таблицы по заголовкам.

Нужно, чтобы один и тот же код работал с эталонным листом («ПФ2»),
тестовой таблицей и чужими файлами, где колонки названы иначе
(«Кол заданий», «Позиция Факт», «Статус итог» и т.п.).
"""

from __future__ import annotations

import re
from typing import Any

from .base import TASK_COLUMNS

ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "ид", "uuid"),
    "date": ("дата", "дата добавления"),
    "marketplace": ("маркетплейс", "площадка", "mp", "площадка/маркетплейс"),
    "sku": ("sku", "артикул", "артикул / sku", "артикул/sku", "ссылка", "url", "товар", "ссылка на товар"),
    "query": ("ключевой запрос", "запрос", "ключ", "ключевое слово", "поисковый запрос"),
    "geo": ("город", "гео", "гео (город)", "локация", "регион"),
    "target_runs": (
        "кол заданий",
        "кол-во заданий",
        "количество заданий",
        "требуется выполнений",
        "кол-во выполнений",
        "количество выполнений",
        "нужно выполнений",
    ),
    "done_runs": ("выполнено", "кол выполненных", "сделано", "прогресс", "выполнено раз"),
    "start_position": ("позиция начальная", "начальная позиция", "стартовая позиция", "позиция на старте"),
    "position": ("позиция факт", "фактическая позиция", "текущая позиция", "позиция"),
    "cart_adds": ("кол добавлений в корзину", "добавлений в корзину", "в корзину", "корзина"),
    "likes": ("кол лайков", "лайков", "лайки", "избранное", "в избранное"),
    "status": ("статус", "статус задания", "состояние"),
    "status_total": ("статус итог", "итоговый статус", "итог"),
    "error": ("ошибка", "ошибки", "комментарий", "причина"),
    "updated_at": ("обновлено", "дата обновления", "время обновления"),
    "filter": ("использовать фильтр", "фильтр"),
    "required_visits": ("необходимое кол заходов", "необходимое количество заходов", "кол заходов"),
}

URL_RE = re.compile(r"-(\d{5,})/?(?:\?|$)")


def normalize(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text


def detect_layout(header: list[Any]) -> dict[str, int]:
    """Логическое поле → номер колонки (1-based). Первое совпадение побеждает."""
    layout: dict[str, int] = {}
    for column, title in enumerate(header, start=1):
        name = normalize(title)
        if not name:
            continue
        for key, aliases in ALIASES.items():
            if key in layout:
                continue
            if name in aliases:
                layout[key] = column
                break

    # Совсем без заголовков — работаем по эталонной раскладке.
    if len(layout) < 3:
        return dict(TASK_COLUMNS)

    # Если отдельного «Статус» нет, используем «Статус итог».
    if "status" not in layout and "status_total" in layout:
        layout["status"] = layout["status_total"]
    return layout


def extract_sku(value: str) -> tuple[str, str | None]:
    """Возвращает (sku, url). Ozon-ссылка → артикул из хвоста, ссылка сохраняется."""
    raw = str(value or "").strip()
    if not raw:
        return "", None
    if raw.startswith("http"):
        match = URL_RE.search(raw)
        return (match.group(1) if match else raw), raw
    return raw, None
