"""Сценарий Ozon (M3): гео → поиск → позиция → карточка → действия.

Селекторы — в `selectors/ozon.json` (сняты с реальной выдачи), каждый шаг имеет
fallback. Необязательные действия деградируют с предупреждением, ключевые
(выдача, позиция, карточка) падают с кодом и артефактами.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any
from urllib.parse import quote

from .. import human
from ..browser import win_window
from ..browser.session import BrowserSession
from ..core.config import AppSettings
from ..core.events import EventBus
from .base import ScenarioError, capture_failure, now_iso, read_for, sku_from_href
from .selectors import SelectorNotFound, SelectorRegistry

BASE = "https://www.ozon.ru"
SEARCH_TIMEOUT = 30000

COLLECT_JS = """
() => {
  const out = [];
  document.querySelectorAll('[data-widget="tileGridDesktop"] a[href*="/product/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\\/product\\/[^/]+-(\\d+)\\//);
    if (m) out.push({ sku: m[1], href });
  });
  return out;
}
"""


def _chance(pct: int) -> bool:
    return random.randint(1, 100) <= max(0, min(100, pct))


async def _collect_cards(
    page: Any, session: BrowserSession | None = None
) -> dict[str, str]:
    """Собирает SKU → href в порядке отрисовки, накапливая батчи при скролле.

    Ozon виртуализирует сетку, поэтому нельзя полагаться на один снимок DOM.
    Через медленный прокси первые 8 карточек приходят сразу, а остальные —
    позже; ранний выход по «двум стабильным итерациям» терял страницу.
    """
    cards: dict[str, str] = {}
    stable = 0
    last_height = 0
    # Дать сетке догрузить первый батч, а не снимать 8 скелетонов.
    await asyncio.sleep(random.uniform(1.5, 2.8))
    for step in range(24):
        challenge = await _find_challenge(page)
        if challenge:
            await _wait_challenge(page, "выдача", session=session)
        batch = await page.evaluate(COLLECT_JS)
        before = len(cards)
        for item in batch:
            cards.setdefault(item["sku"], item["href"])
        try:
            height = int(await page.evaluate("document.documentElement.scrollHeight"))
        except Exception:  # noqa: BLE001
            height = last_height
        if len(cards) == before and height <= last_height:
            stable += 1
        else:
            stable = 0
        last_height = max(last_height, height)
        if stable >= 6:
            break
        await page.mouse.wheel(0, random.randint(700, 1100))
        await asyncio.sleep(random.uniform(1.0, 1.6))
        # Периодически доходим до низа: virtualScroll Ozon подгружает пачку
        # только когда скролл реально у нижней границы.
        if step % 4 == 3:
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(random.uniform(1.2, 2.0))
    return cards


CHALLENGE_MARKERS = (
    "fab_chlg",
    "abt-challenge",
    "variti",
    "antibot",
    "challenge",
    "captcha",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "проверка браузера",
    "слишком много запросов",
    "are you a robot",
)

OVERLAY_ACCEPT = ("Хорошо", "Принять все", "Принять", "Понятно", "Согласен", "OK")
OVERLAY_SKIP = ("Не сейчас", "Позже")


async def _dismiss_overlays(
    page: Any, bus: EventBus | None = None, thread_id: int | None = None
) -> None:
    """Закрывает cookie-баннер и «Добавить адрес»: они перехватывают клики."""
    for label in OVERLAY_ACCEPT:
        try:
            button = page.get_by_role("button", name=label, exact=False)
            if await button.count() and await button.first.is_visible():
                await human.click(page, button.first)
                await human.pause(0.3, 0.8)
                if bus is not None:
                    await bus.log(thread_id, f"Закрыли баннер «{label}»")
                break
        except Exception:  # noqa: BLE001
            continue
    for label in OVERLAY_SKIP:
        try:
            button = page.get_by_role("button", name=label, exact=False)
            if await button.count() and await button.first.is_visible():
                await human.click(page, button.first)
                await human.pause(0.3, 0.8)
                break
        except Exception:  # noqa: BLE001
            continue


async def _wait_card_ready(
    page: Any, bus: EventBus, thread_id: int, timeout: int = 15000
) -> None:
    """Ждёт заголовок карточки: на медленном прокси DOM приходит позже."""
    try:
        await page.wait_for_selector(
            '[data-widget="webProductHeading"], [data-widget="webDetailSKU"], h1',
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        await bus.log(thread_id, f"Карточка: заголовок не появился за {timeout // 1000} с", "warn")
    await human.pause(0.8, 1.8)


async def _find_challenge(page: Any) -> str | None:
    """Ищет челлендж в URL, заголовке, всех фреймах и тексте страницы.

    Проверять только title после загрузки мало: Variti подсовывает iframe и
    меняет разметку уже после первого рендера.
    """

    def hit(value: str) -> bool:
        text = (value or "").lower()
        return any(marker in text for marker in CHALLENGE_MARKERS)

    if hit(page.url):
        return f"URL {page.url[:180]}"

    try:
        frames = page.frames
    except Exception:  # noqa: BLE001
        frames = []
    for frame in frames:
        try:
            if hit(frame.url):
                return f"фрейм {frame.url[:180]}"
        except Exception:  # noqa: BLE001
            continue
    for frame in frames:
        try:
            if frame == page.main_frame:
                continue
            text = (await frame.inner_text("body"))[:800]
            if hit(text):
                return f"фрейм {frame.url[:120]}: {text[:120].strip()}"
        except Exception:  # noqa: BLE001
            continue

    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = ""
    if hit(title):
        return f"заголовок «{title[:120]}»"

    try:
        body = (await page.inner_text("body"))[:2000]
    except Exception:  # noqa: BLE001
        body = ""
    if hit(body):
        return f"текст «{body[:160].strip()}»"
    return None


CHALLENGE_WAIT = 60.0
# Вход через главную и набор запроса. В фикстурах/офлайн-тестах выключается.
HUMAN_SEARCH = True
# Слайдер-пазл Ozon не решается сам: оператор двигает ползунок в окне браузера.
# После одного решения кука ставится и следующие заходы идут без капчи.
CAPTCHA_MANUAL_WAIT = 300.0
SLIDER_MARKERS = ("antibot captcha", "составьте пазл", "подтвердите, что вы не бот")


async def _is_slider_captcha(page: Any) -> bool:
    """Отличает капчу-пазл (нужен человек) от авто-челленджа proof-of-work."""
    try:
        title = (await page.title()).lower()
    except Exception:  # noqa: BLE001
        title = ""
    if any(marker in title for marker in SLIDER_MARKERS):
        return True
    try:
        if await page.locator("#captcha-container, #slider-container, #puzzle").count():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        body = (await page.inner_text("body"))[:1500].lower()
    except Exception:  # noqa: BLE001
        body = ""
    return any(marker in body for marker in SLIDER_MARKERS)


async def _wait_challenge(
    page: Any,
    where: str,
    bus: EventBus | None = None,
    thread_id: int | None = None,
    timeout: float = CHALLENGE_WAIT,
    session: BrowserSession | None = None,
) -> None:
    """Даёт JS-челленджу Ozon (abt-challenge) пройти самому.

    Ozon отдаёт страницу «Antibot Challenge Page» с proof-of-work и через
    2–10 секунд сам делает `location.replace` на исходную выдачу. Если
    проверять заголовок сразу после `domcontentloaded`, любой челлендж
    выглядит как блок и прогон падает, хотя браузер его проходит.
    """
    challenge = await _find_challenge(page)
    if not challenge:
        return
    slider = await _is_slider_captcha(page)
    if slider and timeout < CAPTCHA_MANUAL_WAIT:
        timeout = CAPTCHA_MANUAL_WAIT
    was_visible = bool(session.visible) if session is not None else True
    if bus is not None:
        if slider:
            await bus.log(
                thread_id,
                "Ozon просит капчу-пазл: сдвиньте ползунок в окне браузера, "
                f"ждём до {int(timeout // 60)} мин…",
                "warn",
            )
        else:
            await bus.log(thread_id, f"Ozon проверяет браузер ({where}), ждём автопрохождение…")
    if slider:
        # Показываем окно, даже если оператор его спрятал.
        if session is not None:
            try:
                if win_window.set_visible(session.hwnd, True):
                    session.visible = True
            except Exception:  # noqa: BLE001
                pass
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        challenge = await _find_challenge(page)
        if not challenge:
            if slider and session is not None and not was_visible:
                try:
                    win_window.set_visible(session.hwnd, False)
                    session.visible = False
                except Exception:  # noqa: BLE001
                    pass
            if bus is not None:
                await bus.log(
                    thread_id,
                    "Капча решена, продолжаем" if slider else "Проверка браузера пройдена",
                    "success",
                )
            return
    raise ScenarioError(
        "ozon_blocked",
        f"антибот не пройден за {int(timeout)} с ({where}): {challenge}",
    )


async def _read_watch(
    page: Any,
    seconds: float,
    bus: EventBus | None = None,
    thread_id: int | None = None,
    session: BrowserSession | None = None,
) -> None:
    """Чтение с проверкой капчи: Ozon может показать пазл уже после клика."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        chunk = min(remaining, random.uniform(4.0, 7.0))
        await read_for(page, chunk)
        remaining -= chunk
        if await _find_challenge(page):
            await _wait_challenge(page, "карточка", bus, thread_id, session=session)


async def _search_like_human(
    page: Any,
    query: str,
    bus: EventBus,
    thread_id: int,
    session: BrowserSession | None = None,
) -> bool:
    """Заходит на главную и вводит запрос в строку поиска, как человек.

    Прямой `goto(/search/?text=...)` с холодного профиля — самый заметный
    признак скрипта: нет реферра, нет набора текста, выдача открывается за
    доли секунды. Возвращает False, если ввод не удался (тогда вызывающий
    код откроет выдачу по URL).
    """
    try:
        await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        await _wait_challenge(page, "главная", bus, thread_id, session=session)
        await _dismiss_overlays(page, bus, thread_id)
        await human.pause(1.2, 2.8)
        try:
            await page.mouse.move(
                random.randint(140, 1150),
                random.randint(120, 620),
                steps=random.randint(10, 22),
            )
        except Exception:  # noqa: BLE001
            pass
        await human.pause(0.4, 1.2)

        box = page.locator("input[name='text']").first
        if not await box.count():
            box = page.locator("[data-widget='searchBarDesktop'] input").first
        if not await box.count():
            await bus.log(thread_id, "Поиск: строка не найдена, открываем выдачу по ссылке", "warn")
            return False

        await human.scroll_to(page, box, bias=0.2)
        # type_text сам кликает, печатает по символу и иногда «ошибается».
        await human.type_text(page, box, query)
        await human.pause(0.4, 1.1)
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        await human.pause(0.8, 1.8)
        if "text=" not in page.url and "/search" not in page.url:
            # Иногда первый Enter только выбирает подсказку.
            await page.keyboard.press("Enter")
            await human.pause(1.0, 2.0)
        # Ozon часто заменяет /search/ на предсказанную категорию (/category/...),
        # поэтому признак успеха — уход с главной, а не конкретный путь.
        if page.url.rstrip("/") != BASE and (
            "text=" in page.url or "/search" in page.url or "/category/" in page.url
        ):
            await bus.log(thread_id, "Поиск: запрос введён в строку Ozon")
            return True
        await bus.log(thread_id, "Поиск: ввод не привёл к выдаче, открываем по ссылке", "warn")
        return False
    except Exception as exc:  # noqa: BLE001
        await bus.log(
            thread_id,
            f"Поиск: ввод не удался ({type(exc).__name__}), открываем по ссылке",
            "warn",
        )
        return False


async def _open_search(
    page: Any,
    query: str,
    page_no: int,
    bus: EventBus,
    thread_id: int,
    listing_url: str | None = None,
    session: BrowserSession | None = None,
) -> None:
    # Ozon редиректит /search/ на предсказанную категорию. Дальше листаем уже
    # её, иначе страница 2 может уйти в другой набор товаров.
    url = listing_url or f"{BASE}/search/?text={quote(query)}&from_global=true"
    if page_no > 1:
        if "page=" in url:
            url = re.sub(r"([?&])page=\d+", rf"\g<1>page={page_no}", url)
        else:
            url += f"&page={page_no}"

    typed = False
    if page_no == 1 and not listing_url and session is not None and HUMAN_SEARCH:
        typed = await _search_like_human(page, query, bus, thread_id, session=session)
    if typed:
        status = 200
    else:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = response.status if response is not None else None
    await _wait_challenge(page, "выдача", bus, thread_id, session=session)
    try:
        await page.wait_for_selector('[data-widget="tileGridDesktop"]', timeout=SEARCH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        challenge = await _find_challenge(page)
        if challenge:
            await _wait_challenge(page, "выдача", bus, thread_id, session=session)
            try:
                await page.wait_for_selector('[data-widget="tileGridDesktop"]', timeout=SEARCH_TIMEOUT)
            except Exception as retry_exc:  # noqa: BLE001
                raise ScenarioError(
                    "ozon_blocked", f"антибот на выдаче: {challenge}"
                ) from retry_exc
        else:
            body = ""
            try:
                body = (await page.inner_text("body"))[:1000].lower()
            except Exception:  # noqa: BLE001
                pass
            if (
                status == 403
                or "доступ ограничен" in body
                or "vpn" in body
                or "нет соединения" in body
                or "incident" in body
            ):
                raise ScenarioError(
                    "ozon_blocked",
                    f"Ozon ограничил доступ (HTTP {status}): нужен другой IP/прокси и пауза",
                ) from exc
            raise ScenarioError("search_empty", f"нет сетки результатов: {exc}") from exc
    await _dismiss_overlays(page, bus, thread_id)
    await human.pause(0.7, 1.5)


async def _apply_geo(
    page: Any,
    geo: str,
    bus: EventBus,
    thread_id: int,
    session: BrowserSession | None = None,
) -> None:
    if not geo:
        return
    if geo.startswith("http"):
        await bus.log(thread_id, f"Гео: {geo}")
        await page.goto(geo, wait_until="domcontentloaded", timeout=45000)
        await _wait_challenge(page, "гео", bus, thread_id, session=session)
        await human.pause(1.0, 2.0)
        for label in ("Да, я здесь", "Продолжить", "Применить"):
            try:
                button = page.get_by_role("button", name=label)
                if await button.count() and await button.first.is_visible():
                    await human.click(page, button.first)
                    await human.pause(0.6, 1.2)
                    break
            except Exception:  # noqa: BLE001
                continue
    else:
        await bus.log(thread_id, f"Гео задано строкой («{geo[:40]}»), пропускаем навигацию", "warn")


async def find_position(
    page: Any,
    query: str,
    target_sku: str,
    max_pages: int,
    bus: EventBus,
    thread_id: int,
    session: BrowserSession | None = None,
) -> tuple[int | None, int, str | None]:
    # Ozon для поиска/категории использует virtualScroll: `&page=2` без
    # search_page_state возвращает ту же первую страницу. Поэтому не листаем
    # URL, а несколько раз доскролливаем и накапливаем карточки.
    await _open_search(page, query, 1, bus, thread_id, session=session)
    cards: dict[str, str] = {}
    for round_no in range(1, max_pages + 1):
        before = len(cards)
        batch = await _collect_cards(page, session=session)
        if not batch and not cards:
            raise ScenarioError("search_empty", "выдача пуста или не отрисовалась")
        for sku, href in batch.items():
            cards.setdefault(sku, href)
        await bus.log(thread_id, f"Подгрузка {round_no}: карточек {len(cards)}")
        if target_sku in cards:
            position = list(cards).index(target_sku) + 1
            return position, 1, cards[target_sku]
        if len(cards) == before:
            await bus.log(thread_id, "Подгрузка закончилась: новые карточки не появляются")
            break
        if round_no < max_pages:
            await human.pause(0.6, 1.4)

    return None, 0, None


UNAVAILABLE_MARKERS = (
    "товар закончился",
    "товар не найден",
    "снят с продажи",
    "больше не продается",
    "больше не продаётся",
)


async def _product_unavailable(
    page: Any,
    url: str,
    bus: EventBus,
    thread_id: int,
    session: BrowserSession | None = None,
) -> str | None:
    """Проверяет карточку, если SKU не нашёлся в выдаче.

    Ozon у снятого с продажи товара редиректит карточку в поиск и пишет
    «Товар закончился». Такую строку бессмысленно гонять 10 раз.
    """
    if not url:
        return None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await _wait_challenge(page, "товар", bus, thread_id, session=session)
        await human.pause(1.0, 2.0)
        try:
            body = (await page.inner_text("body"))[:4000].lower()
        except Exception:  # noqa: BLE001
            body = ""
        for marker in UNAVAILABLE_MARKERS:
            if marker in body:
                return marker
        heading = 0
        try:
            heading = await page.locator('[data-widget="webProductHeading"]').count()
        except Exception:  # noqa: BLE001
            pass
        if "/search" in page.url and not heading:
            return "карточка перенаправляет в поиск (товар недоступен)"
    except Exception as exc:  # noqa: BLE001
        await bus.log(thread_id, f"Проверка наличия: {type(exc).__name__}: {exc}", "warn")
    return None


async def _open_card(
    page: Any,
    sku: str,
    href: str | None,
    bus: EventBus,
    thread_id: int,
    session: BrowserSession | None = None,
) -> None:
    absolute = f"{BASE}{href}" if href and href.startswith("/") else (href or f"{BASE}/product/-{sku}/")
    locator = page.locator(f'a[href*="-{sku}/"]').first
    try:
        if await locator.count():
            # Ozon часто ставит target="_blank": клик открывает вторую вкладку,
            # а наш fallback goto — ещё одну с тем же товаром. Убираем target
            # и закрываем всё, что всё-таки открылось.
            try:
                await locator.evaluate(
                    "el => { el.removeAttribute('target'); el.removeAttribute('rel'); }"
                )
            except Exception:  # noqa: BLE001
                pass
            context = page.context
            before_pages = set(context.pages)
            await human.scroll_to(page, locator, bias=0.4)
            await human.click(page, locator)
            await human.pause(0.8, 1.6)
            opened_url = None
            for extra in [p for p in context.pages if p not in before_pages]:
                try:
                    await extra.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if sku in extra.url:
                        opened_url = extra.url
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await extra.close()
                except Exception:  # noqa: BLE001
                    pass
            if opened_url and sku not in page.url:
                await page.goto(opened_url, wait_until="domcontentloaded", timeout=60000)
            await _wait_challenge(page, "карточка", bus, thread_id, session=session)
            if sku in page.url:
                await _dismiss_overlays(page, bus, thread_id)
                return
    except ScenarioError:
        raise
    except Exception as exc:  # noqa: BLE001
        await bus.log(thread_id, f"Клик по карточке не удался ({exc}), открываем URL", "warn")

    await page.goto(absolute, wait_until="domcontentloaded", timeout=60000)
    await human.pause(0.8, 1.6)
    await _wait_challenge(page, "карточка", bus, thread_id, session=session)
    await _dismiss_overlays(page, bus, thread_id)


async def _click_optional(
    registry: SelectorRegistry,
    page: Any,
    key: str,
    label: str,
    bus: EventBus,
    thread_id: int,
) -> bool:
    try:
        locator = await registry.first(page, key, timeout_ms=3000, required=False)
        if locator is None:
            await bus.log(thread_id, f"{label}: элемент не найден, пропускаем", "warn")
            return False
        await human.scroll_to(page, locator, bias=0.3)
        await human.click(page, locator)
        await human.pause(0.5, 1.2)
        return True
    except Exception as exc:  # noqa: BLE001
        await bus.log(thread_id, f"{label}: {type(exc).__name__}: {exc}", "warn")
        return False


async def _browse_block(
    registry: SelectorRegistry,
    page: Any,
    key: str,
    label: str,
    minutes: float,
    bus: EventBus,
    thread_id: int,
    session: BrowserSession | None = None,
) -> bool:
    heading = await registry.first(page, key, timeout_ms=3500, required=False)
    if heading is None:
        await bus.log(thread_id, f"{label}: блок не найден", "warn")
        return False
    try:
        await human.scroll_to(page, heading, bias=0.3)
        await human.pause(0.5, 1.0)
        container = heading.locator("xpath=ancestor::div[.//a[contains(@href,'/product/')]][1]")
        link = container.locator("a[href*='/product/']").first
        if await link.count():
            await human.click(page, link)
            await human.pause(1.0, 2.0)
            await human.scroll(page, random.randint(2, 4))
            await _read_watch(page, minutes, bus, thread_id, session)
            await page.go_back(wait_until="domcontentloaded")
            await human.pause(0.8, 1.5)
        else:
            await bus.log(thread_id, f"{label}: товаров внутри не нашлось, только просмотр")
            await _read_watch(page, minutes, bus, thread_id, session)
        return True
    except Exception as exc:  # noqa: BLE001
        await bus.log(thread_id, f"{label}: {type(exc).__name__}: {exc}", "warn")
        return False


async def _view_photos(registry: SelectorRegistry, page: Any, bus: EventBus, thread_id: int) -> None:
    gallery = await registry.first(page, "gallery", timeout_ms=8000, required=False)
    if gallery is None:
        await bus.log(thread_id, "Фото: галерея ещё не догрузилась, ждём…", "warn")
        await human.pause(1.2, 2.2)
        gallery = await registry.first(page, "gallery", timeout_ms=8000, required=False)
    if gallery is None:
        await bus.log(thread_id, "Фото: галерея не найдена", "warn")
        return
    try:
        await human.scroll_to(page, gallery, bias=0.25)
        await human.pause(0.4, 0.9)

        imgs = gallery.locator("img")
        total = await imgs.count()
        main = None
        for index in range(min(total, 20)):
            box = await imgs.nth(index).bounding_box()
            if box and box["width"] > 150 and box["height"] > 150:
                main = imgs.nth(index)
                break
        if main is None:
            main = imgs.first
        # Клик по большой картинке открывает полноэкранный просмотрщик
        # (`img[src*='wc2500']`), а не просто выделяет миниатюру.
        viewed = 0
        opened = _chance(75)
        if opened:
            await human.click(page, main)
            await human.pause(0.8, 1.4)

            viewer = page.locator("img[src*='wc2500']")
            if await viewer.count():
                viewed = 1
                for _ in range(random.randint(1, 4)):
                    await page.keyboard.press("ArrowRight")
                    await human.pause(0.5, 0.9)
                    viewed += 1
            await page.keyboard.press("Escape")
            await human.pause(0.4, 0.8)
            # Просмотрщик иногда не закрывается с первого Escape: если оставить
            # его открытым, карточка «замирает» — клики и скролл уходят в оверлей.
            for _ in range(2):
                if not await page.locator(".vue-portal-target img").count():
                    break
                await page.keyboard.press("Escape")
                await human.pause(0.3, 0.7)
            if await page.locator(".vue-portal-target img").count():
                for selector in ("button[aria-label*='Закрыть']", ".vue-portal-target button[class*='close']"):
                    try:
                        button = page.locator(selector).first
                        if await button.count() and await button.is_visible():
                            await human.click(page, button)
                            break
                    except Exception:  # noqa: BLE001
                        continue
                await human.pause(0.3, 0.6)
                if await page.locator(".vue-portal-target img").count():
                    await bus.log(thread_id, "Фото: просмотрщик не закрылся, продолжаем", "warn")
        else:
            # Иногда достаточно посмотреть превью и пролистать, без просмотрщика.
            await human.pause(0.6, 1.4)
            await human.wheel(page, random.randint(150, 400))
            await human.pause(0.4, 1.0)
            await human.wheel(page, -random.randint(100, 300))
        await bus.log(thread_id, f"Просмотрели фото: {viewed or 'галерея'}")
    except Exception as exc:  # noqa: BLE001
        await bus.log(thread_id, f"Фото: {type(exc).__name__}: {exc}", "warn")


async def _like_review(
    registry: SelectorRegistry, page: Any, bus: EventBus, thread_id: int
) -> bool:
    locator = await registry.first(page, "review_like", timeout_ms=2500, required=False)
    if locator is None:
        await bus.log(thread_id, "Лайк отзыва: кнопка не найдена", "warn")
        return False
    try:
        await human.click(page, locator)
        await bus.log(thread_id, "Лайкнули отзыв")
        return True
    except Exception as exc:  # noqa: BLE001
        await bus.log(thread_id, f"Лайк отзыва: {type(exc).__name__}: {exc}", "warn")
        return False


async def run_ozon(
    session: BrowserSession,
    task: dict[str, Any],
    settings: AppSettings,
    bus: EventBus,
    thread_id: int,
) -> dict[str, Any]:
    registry = SelectorRegistry.load("ozon")
    page = session.page
    page.set_default_timeout(15000)

    view = settings.view
    steps: list[dict[str, Any]] = []
    started = time.monotonic()
    total_dwell = random.uniform(*view.card_time_sec)
    sku = str(task["sku"])
    query = str(task["query"])

    def mark(step: str, **extra: Any) -> None:
        steps.append({"step": step, "at": now_iso(), **extra})

    try:
        await _apply_geo(page, str(task.get("geo") or ""), bus, thread_id, session=session)
        mark("geo")

        mark("search_opened", query=query)

        position, found_page, href = await find_position(
            page, query, sku, view.max_search_pages, bus, thread_id, session=session
        )
        if position is None:
            unavailable = await _product_unavailable(
                page, str(task.get("url") or ""), bus, thread_id, session=session
            )
            if unavailable:
                raise ScenarioError(
                    "product_unavailable",
                    f"SKU {sku} недоступен: {unavailable}",
                )
            raise ScenarioError(
                "position_not_found",
                f"SKU {sku} не найден за {view.max_search_pages} подгрузок по запросу «{query}»",
            )
        await bus.log(thread_id, f"Позиция товара: {position} (стр. {found_page})", "success")
        mark("position", position=position, page=found_page)

        # Не кликаем карточку сразу после скролла: человек изучает выдачу.
        await human.pause(1.5, 4.0)
        if _chance(50):
            await human.wheel(page, -random.randint(250, 700))
            await human.pause(0.5, 1.4)

        href = href or str(task.get("url") or "")
        await _open_card(page, sku, href, bus, thread_id, session=session)
        await _wait_card_ready(page, bus, thread_id)
        await bus.log(thread_id, "Карточка открыта: фото → описание → отзывы")
        mark("card_opened", url=page.url)

        if settings.general.quick_actions.view_photos and _chance(80):
            await bus.log(thread_id, "Фото: открываем галерею…")
            await _view_photos(registry, page, bus, thread_id)
            mark("photos")
        elif settings.general.quick_actions.view_photos:
            await bus.log(thread_id, "Фото: пропускаем в этом заходе")

        description = await registry.first(page, "description", timeout_ms=8000, required=False)
        if description is None:
            await bus.log(thread_id, "Описание: прокручиваем, блок ещё не догрузился…")
            await human.scroll(page, random.randint(1, 3))
            await human.pause(1.0, 2.0)
            description = await registry.first(page, "description", timeout_ms=8000, required=False)
        if description is not None:
            await bus.log(thread_id, "Читаем описание…")
            await human.scroll_to(page, description, bias=0.3)
            await _read_watch(page, total_dwell * random.uniform(0.12, 0.2), bus, thread_id, session)
            await bus.log(thread_id, "Описание прочитано")
            mark("description")
        else:
            await bus.log(thread_id, "Описание: блок не найден", "warn")

        # Отзывы открываются отдельной страницей: на карточке виджет пустой,
        # пока не нажмёшь вкладку, а прямой URL стабильно отдаёт список.
        # Заходим не всегда: постоянный `/reviews/` в каждом сеансе — паттерн.
        if _chance(75):
            try:
                await bus.log(thread_id, "Открываем отзывы…")
                reviews_url = page.url.split("?")[0].split("#")[0].rstrip("/") + "/reviews/"
                await page.goto(reviews_url, wait_until="domcontentloaded", timeout=60000)
                await _wait_challenge(page, "отзывы", bus, thread_id, session=session)
                await human.pause(0.8, 1.5)
                reviews = await registry.first(page, "reviews", timeout_ms=8000, required=False)
                if reviews is None:
                    await human.pause(1.2, 2.2)
                    reviews = await registry.first(page, "reviews", timeout_ms=8000, required=False)
                if reviews is not None:
                    await human.scroll_to(page, reviews, bias=0.2)
                    read_seconds = random.uniform(*view.reviews_time_sec)
                    await bus.log(thread_id, f"Читаем отзывы ~{read_seconds:.0f} с…")
                    await _read_watch(page, read_seconds, bus, thread_id, session)
                    await bus.log(thread_id, "Отзывы прочитаны")
                    mark("reviews", seconds=round(read_seconds, 1), url=page.url)
                    if _chance(view.review_like_pct):
                        await _like_review(registry, page, bus, thread_id)
                        mark("review_like")
                    await page.go_back(wait_until="domcontentloaded")
                    await human.pause(1.0, 1.8)
                else:
                    await bus.log(thread_id, "Отзывы: страница открылась без списка", "warn")
            except Exception as exc:  # noqa: BLE001
                await bus.log(thread_id, f"Отзывы: {type(exc).__name__}: {exc}", "warn")
        else:
            await bus.log(thread_id, "Отзывы: пропускаем в этом заходе")

        liked = 0
        if _chance(view.like_pct):
            if await registry.exists(page, "favorite_active"):
                await bus.log(thread_id, "Товар уже в избранном")
            elif await _click_optional(registry, page, "add_to_favorite", "Избранное", bus, thread_id):
                liked = 1
                mark("favorite")

        if _chance(view.compare_pct):
            if await _click_optional(registry, page, "add_to_compare", "Сравнение", bus, thread_id):
                mark("compare")

        if _chance(view.see_also_pct):
            await _browse_block(
                registry, page, "see_also", "«Рекомендуем также»",
                random.uniform(8, 20), bus, thread_id, session=session,
            )
            mark("see_also")

        if _chance(view.other_sellers_pct):
            await _browse_block(
                registry, page, "other_sellers", "«Другие продавцы»",
                random.uniform(8, 20), bus, thread_id, session=session,
            )
            mark("other_sellers")

        # Корзина — на карточке, после «прогулки» возвращаемся.
        if sku not in page.url:
            await _open_card(page, sku, href, bus, thread_id, session=session)

        cart = 0
        if _chance(view.cart_pct):
            if await _click_optional(registry, page, "add_to_cart", "Корзина", bus, thread_id):
                cart = 1
                mark("cart")

        elapsed = time.monotonic() - started
        remaining = total_dwell - elapsed
        if remaining > 3:
            await bus.log(thread_id, f"Дочитываем карточку ~{remaining:.0f} с…")
            await _read_watch(page, remaining, bus, thread_id, session)
            mark("dwell", seconds=round(elapsed + remaining, 1))
        await bus.log(
            thread_id,
            f"Карточка отработана: позиция {position}, корзина {cart}, избранное {liked}",
            "success",
        )
        return {
            "position": position,
            "page": found_page,
            "cart": cart,
            "liked": liked,
            "dwell": round(total_dwell, 1),
            "steps": steps,
        }

    except SelectorNotFound as exc:
        artifact = await capture_failure(page, f"ozon-{task['id']}-{exc.key}")
        raise ScenarioError("selector_not_found", str(exc), artifact) from exc
    except ScenarioError as exc:
        if exc.artifact is None:
            exc.artifact = await capture_failure(page, f"ozon-{task['id']}-{exc.code}")
        raise
    except Exception as exc:  # noqa: BLE001
        artifact = await capture_failure(page, f"ozon-{task['id']}-crash")
        raise ScenarioError("ozon_crash", f"{type(exc).__name__}: {exc}", artifact) from exc
