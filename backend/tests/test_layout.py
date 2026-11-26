"""Раскладка колонок: чужая таблица подхватывается по заголовкам.

python tests/test_layout.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sheets.base import TASK_HEADER  # noqa: E402
from app.sheets.layout import detect_layout, extract_sku  # noqa: E402

USER_HEADER = [
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


def main() -> None:
    layout = detect_layout(USER_HEADER)
    assert layout["date"] == 1, layout
    assert layout["sku"] == 2
    assert layout["query"] == 3
    assert layout["filter"] == 4
    assert layout["required_visits"] == 5
    assert layout["geo"] == 6
    assert layout["target_runs"] == 7
    assert layout["start_position"] == 8
    assert layout["position"] == 9
    assert layout["cart_adds"] == 10
    assert layout["likes"] == 11
    assert layout["status_total"] == 12
    assert layout["status"] == 13
    assert "id" not in layout, "в этой таблице нет колонки ID"
    assert "done_runs" not in layout, "в этой таблице нет счётчика выполнений"

    standard = detect_layout(TASK_HEADER)
    assert standard["id"] == 1 and standard["sku"] == 3 and standard["status"] == 12
    assert standard["done_runs"] == 7 and standard["position"] == 9

    # Опечатки/регистр/ё не ломают распознавание.
    soft = detect_layout(["id", "маркетплейс", "sku", "ключевой запрос", "ГЕО", "требуется выполнений"])
    assert soft["geo"] == 5 and soft["target_runs"] == 6

    sku, url = extract_sku(
        "https://www.ozon.ru/product/lampa-svetodiodnaya-gu10-3940020239/?at=1k&sh=abc"
    )
    assert sku == "3940020239" and url and url.startswith("https://")
    plain, no_url = extract_sku("3940020239")
    assert plain == "3940020239" and no_url is None
    assert extract_sku("") == ("", None)

    print("layout: ok — колонки чужой таблицы, эталонный лист, извлечение SKU из ссылки")


if __name__ == "__main__":
    main()
