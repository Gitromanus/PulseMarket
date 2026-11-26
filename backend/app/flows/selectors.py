"""Реестр селекторов с fallback-цепочкой и воспроизводимыми ошибками.

Задача — не «угадать» вёрстку, а явно показать, какой из вариантов сработал,
и упасть с кодом `selector_not_found`, если не сработал ни один.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

SELECTOR_DIR = Path(__file__).resolve().parent / "selectors"


class SelectorNotFound(RuntimeError):
    def __init__(self, key: str, attempts: list[str]) -> None:
        self.key = key
        self.attempts = attempts
        super().__init__(f"selector_not_found: {key} (пробовали {len(attempts)} вариантов)")


class SelectorRegistry:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data

    @classmethod
    def load(cls, name: str) -> "SelectorRegistry":
        path = SELECTOR_DIR / f"{name}.json"
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def keys(self) -> list[str]:
        return list(self.data)

    def strategies(self, key: str) -> list[dict[str, Any]]:
        return self.data.get(key, [])

    def _locator(self, page: Page, strategy: dict[str, Any]) -> tuple[Locator | None, str]:
        widget = strategy.get("widget")
        base: Any = page.locator(f'[data-widget="{widget}"]') if widget else page
        css = strategy.get("css")
        text = strategy.get("text")
        aria = strategy.get("aria")

        if css:
            return base.locator(css).first, f"widget={widget or '-'} css={css}"
        if aria:
            return base.locator(f'[aria-label*="{aria}"]').first, f"aria={aria}"
        if text:
            return page.get_by_text(text, exact=False).first, f"text={text}"
        if widget:
            return base.first, f"widget={widget}"
        return None, "empty"

    async def first(
        self,
        page: Page,
        key: str,
        *,
        timeout_ms: int = 3500,
        required: bool = True,
    ) -> Locator | None:
        """Первый видимый кандидат. Порядок вариантов = порядок в JSON."""
        attempts: list[str] = []
        for strategy in self.strategies(key):
            locator, label = self._locator(page, strategy)
            attempts.append(label)
            if locator is None:
                continue
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:  # noqa: BLE001 — пробуем следующий вариант
                continue
        if required:
            raise SelectorNotFound(key, attempts)
        return None

    async def exists(self, page: Page, key: str, timeout_ms: int = 1500) -> bool:
        return await self.first(page, key, timeout_ms=timeout_ms, required=False) is not None
