"""Проверка реестра селекторов на синтетической странице.

python tests/test_selectors.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

from app.flows.base import sku_from_href  # noqa: E402
from app.flows.selectors import SelectorNotFound, SelectorRegistry  # noqa: E402

HTML = """
<!doctype html><html><body>
  <div data-widget="searchBarDesktop"><input name="text"></div>
  <div data-widget="webAddToCart"><button>В корзину</button></div>
  <div data-widget="webListReviews">
     <button aria-label="Нравится">👍</button>
  </div>
  <div><h2>Смотрите также</h2><a href="/product/x-111/">товар</a></div>
</body></html>
"""


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        registry = SelectorRegistry.load("ozon")
        assert {"search_input", "add_to_cart", "review_like", "see_also"} <= set(registry.keys())

        await page.set_content(HTML)
        assert await registry.first(page, "search_input")
        cart = await registry.first(page, "add_to_cart")
        assert cart is not None and (await cart.inner_text()).strip() == "В корзину"
        assert await registry.first(page, "reviews")
        assert await registry.first(page, "review_like")
        assert await registry.first(page, "see_also")

        # Нет элемента: optional -> None, required -> SelectorNotFound.
        assert await registry.first(page, "other_sellers", timeout_ms=250, required=False) is None
        try:
            await registry.first(page, "other_sellers", timeout_ms=250)
        except SelectorNotFound as exc:
            assert exc.key == "other_sellers"
        else:
            raise AssertionError("ожидали SelectorNotFound")

        # Fallback: виджета нет, но текст есть — второй/третий вариант срабатывает.
        await page.set_content("<button>Добавить в корзину</button>")
        assert await registry.first(page, "add_to_cart", timeout_ms=1500)

        # Разбор SKU из ссылок Ozon.
        assert sku_from_href("/product/naushniki-air-pro-3-5682635133/?at=abc") == "5682635133"
        assert sku_from_href("/product/x/?at=1") is None
        assert sku_from_href(None) is None

        await browser.close()

    print("selectors: ok — порядок fallback, optional/required, разбор SKU")


if __name__ == "__main__":
    asyncio.run(main())
