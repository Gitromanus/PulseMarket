"""Humanizer: движение мыши, печать с опечатками, инерционный скролл, паузы.

Не «обманывает антифрод» сам по себе — снижает шум поверх нормального отпечатка.
Все тайминги рандомизированы, фиксированных sleep в сценариях быть не должно.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

RU_ALPHABET = "йцукенгшщзхъфывапролджэячсмитьбю"

# Последняя позиция курсора на странице: без этого каждый move стартовал из
# случайной точки — на экране это выглядело как телепорт.
_MOUSE: dict[int, tuple[float, float]] = {}


def _viewport(page: Any) -> dict[str, int]:
    return page.viewport_size or {"width": 1280, "height": 800}


def cursor(page: Any) -> tuple[float, float] | None:
    """Последняя позиция виртуального курсора (CSS-пиксели viewport)."""
    return _MOUSE.get(id(page))


def set_cursor(page: Any, x: float, y: float) -> None:
    _MOUSE[id(page)] = (float(x), float(y))


async def pause(lo: float = 0.25, hi: float = 0.9) -> float:
    delay = random.uniform(lo, hi)
    await asyncio.sleep(delay)
    return delay


async def wheel(page: Any, dy: float) -> None:
    """Прокрутка рваными мелкими шагами: колесо не «прыгает» на пол-экрана."""
    if abs(dy) < 1:
        return
    steps = max(5, min(30, int(abs(dy) / 55)))
    chunk = dy / steps
    for _ in range(steps):
        await page.mouse.wheel(0, chunk * random.uniform(0.85, 1.15))
        await asyncio.sleep(random.uniform(0.010, 0.030))
    await asyncio.sleep(random.uniform(0.05, 0.18))


async def scroll_to(page: Any, locator: Any, bias: float = 0.35) -> None:
    """Докручивает до элемента колесом, а не мгновенным scrollIntoView."""
    try:
        box = await locator.bounding_box()
    except Exception:  # noqa: BLE001
        box = None
    if not box:
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        return
    viewport = _viewport(page)
    delta = box["y"] - viewport["height"] * bias
    if abs(delta) > 25:
        await wheel(page, delta)
        await pause(0.15, 0.4)


async def move(page: Any, x: float, y: float) -> None:
    viewport = _viewport(page)
    key = id(page)
    start = _MOUSE.get(key)
    if start is None:
        start = (
            random.uniform(viewport["width"] * 0.15, viewport["width"] * 0.35),
            random.uniform(viewport["height"] * 0.35, viewport["height"] * 0.65),
        )
    start_x, start_y = start
    # Кривая с контрольной точкой + лёгкий «перелёт» и коррекция.
    ctrl_x = (start_x + x) / 2 + random.uniform(-120, 120)
    ctrl_y = (start_y + y) / 2 + random.uniform(-90, 90)

    steps = random.randint(18, 32)
    duration = random.uniform(0.35, 0.85)
    for step in range(1, steps + 1):
        t = step / steps
        eased = t * t * (3 - 2 * t)
        px = (1 - eased) ** 2 * start_x + 2 * (1 - eased) * eased * ctrl_x + eased**2 * x
        py = (1 - eased) ** 2 * start_y + 2 * (1 - eased) * eased * ctrl_y + eased**2 * y
        jx = px + random.uniform(-1.2, 1.2)
        jy = py + random.uniform(-1.2, 1.2)
        await page.mouse.move(jx, jy)
        _MOUSE[key] = (jx, jy)
        await asyncio.sleep(duration / steps * random.uniform(0.7, 1.4))

    _MOUSE[key] = (x, y)
    await pause(0.06, 0.2)


async def click(page: Any, locator: Any, timeout: int = 15000) -> None:
    await locator.wait_for(state="visible", timeout=timeout)
    await scroll_to(page, locator)
    box = await locator.bounding_box()
    if not box:
        await locator.click(timeout=timeout)
        return
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    await move(page, x, y)
    await page.mouse.down()
    await pause(0.05, 0.15)
    await page.mouse.up()


async def type_text(page: Any, locator: Any, text: str) -> None:
    await click(page, locator)
    await pause(0.15, 0.4)

    for char in text:
        if char.isalpha() and random.random() < 0.04:
            wrong = random.choice(RU_ALPHABET if char.lower() in RU_ALPHABET else "qwerty")
            await page.keyboard.type(wrong)
            await asyncio.sleep(random.uniform(0.09, 0.22))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.06, 0.15))

        await page.keyboard.type(char)
        base = 0.06 if char == " " else 0.1
        await asyncio.sleep(random.uniform(base * 0.55, base * 2.1))

    await pause(0.2, 0.6)


async def scroll(page: Any, steps: int | None = None) -> int:
    total = steps if steps is not None else random.randint(3, 7)
    scrolled = 0
    for _ in range(total):
        delta = random.randint(180, 520)
        await wheel(page, delta)
        scrolled += delta
        await asyncio.sleep(random.uniform(0.25, 0.95))
        if random.random() < 0.12:
            back = random.randint(80, 220)
            await wheel(page, -back)
            scrolled -= back
            await asyncio.sleep(random.uniform(0.2, 0.6))
    return scrolled


async def read(page: Any, lo: float = 2.0, hi: float = 6.0) -> None:
    """«Человек читает»: пауза плюс микродвижения."""
    deadline = random.uniform(lo, hi)
    elapsed = 0.0
    while elapsed < deadline:
        chunk = random.uniform(0.4, 1.2)
        await asyncio.sleep(min(chunk, deadline - elapsed))
        elapsed += chunk
        if random.random() < 0.35:
            await wheel(page, random.randint(-60, 120))
