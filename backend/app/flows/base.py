"""Общие помощники сценариев: ошибки с кодами, артефакты, «чтение»."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.config import app_data_dir


class ScenarioError(RuntimeError):
    def __init__(self, code: str, message: str, artifact: str | None = None) -> None:
        self.code = code
        self.artifact = artifact
        super().__init__(f"{code}: {message}")


async def capture_failure(page: Any, slug: str) -> str | None:
    """Скриншот + HTML при ошибке: без них разбор инцидента превращается в гадание."""
    try:
        folder = app_data_dir() / "artifacts" / f"{slug}_{datetime.now().strftime('%m%d-%H%M%S')}"
        folder.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(folder / "page.png"), full_page=False)
        html = await page.content()
        (folder / "page.html").write_text(html, encoding="utf-8")
        (folder / "url.txt").write_text(page.url, encoding="utf-8")
        try:
            text = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        (folder / "final_text.txt").write_text(text[:5000], encoding="utf-8")
        (folder / "final_url.txt").write_text(page.url, encoding="utf-8")
        try:
            frames = "\n".join(frame.url for frame in page.frames)
        except Exception:  # noqa: BLE001
            frames = ""
        (folder / "frames.txt").write_text(frames, encoding="utf-8")
        return str(folder)
    except Exception:  # noqa: BLE001 — артефакты не должны маскировать исходную ошибку
        return None


async def read_for(page: Any, seconds: float) -> None:
    """Пауза «человек читает»: микродвижения и прокрутка, без фиксированного sleep."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        chunk = min(remaining, random.uniform(0.5, 1.6))
        await asyncio.sleep(chunk)
        remaining -= chunk
        if random.random() < 0.4:
            try:
                await page.mouse.wheel(0, random.randint(-80, 160))
            except Exception:  # noqa: BLE001
                return


def sku_from_href(href: str | None) -> str | None:
    if not href:
        return None
    match = re.search(r"/product/[^/]+-(\d+)/", href)
    return match.group(1) if match else None


def now_iso() -> str:
    return datetime.now().strftime("%H:%M:%S")
