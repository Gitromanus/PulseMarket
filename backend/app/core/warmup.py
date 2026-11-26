"""Прогрев новых профилей: по одному открыть Ozon, полистать и закрыть.

Нужен после генератора профилей: пустая папка Chrome не содержит куков, и
первый заход в Ozon выглядит как «совсем новый» браузер. Прогрев даёт профилю
историю и куки, чтобы следующие прогоны были менее подозрительными.

Не пересекается с очередью: запускается только когда Supervisor в idle и
пропускает профили, занятые прогоном или ручным режимом.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..browser import win_window
from ..browser.profiles import Profile, ProfileStore
from ..browser.providers import BrowserProvider, build_provider
from ..browser.proxies import Proxy, ProxyAssignments, ProxyPool
from ..browser.session import BrowserSession
from ..core.config import AppSettings
from ..core.events import EventBus
from ..flows.ozon import BASE, _dismiss_overlays, _wait_challenge
from .. import human

WARM_QUERIES = (
    "наушники беспроводные",
    "лампа настольная",
    "чехол для телефона",
    "кроссовки мужские",
    "крем для лица",
    "мышь беспроводная",
)


class WarmupManager:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "running": False,
            "total": 0,
            "done": 0,
            "failed": 0,
            "current": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def status(self) -> dict[str, Any]:
        return dict(self._state)

    def _candidates(self, settings: AppSettings, busy_paths: set[str]) -> list[Profile]:
        store = ProfileStore(settings)
        busy = {path.lower() for path in busy_paths}
        return [
            profile
            for profile in store.list("active")
            if profile.is_new and str(profile.folder).lower() not in busy
        ]

    async def start(
        self,
        settings: AppSettings,
        count: int,
        busy_paths: set[str] | None = None,
    ) -> dict[str, Any]:
        if self._state["running"]:
            return {"ok": False, "error": "Прогрев уже идёт"}
        candidates = self._candidates(settings, busy_paths or set())
        if not candidates:
            return {"ok": False, "error": "Нет новых профилей: все уже запускались"}
        chosen = candidates[:count]
        self._stop.clear()
        self._state = {
            "running": True,
            "total": len(chosen),
            "done": 0,
            "failed": 0,
            "current": None,
            "error": None,
            "started_at": datetime.now().strftime("%H:%M:%S"),
            "finished_at": None,
        }
        self._task = asyncio.create_task(self._run(settings, chosen), name="warmup")
        await self.bus.log(
            None,
            f"Прогрев профилей: {len(chosen)} шт. "
            f"({chosen[0].label} … {chosen[-1].label}), по одному",
        )
        return {
            "ok": True,
            "total": len(chosen),
            "names": [profile.label for profile in chosen],
            "status": self.status(),
        }

    async def stop(self) -> dict[str, Any]:
        if not self._state["running"]:
            return {"ok": False, "error": "Прогрев не запущен"}
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return {"ok": True, "status": self.status()}

    async def _run(self, settings: AppSettings, profiles: list[Profile]) -> None:
        provider: BrowserProvider | None = None
        pool = ProxyPool(settings.general.proxies_file or None)
        assignments = ProxyAssignments()
        try:
            provider = build_provider(settings.browser)
            for index, profile in enumerate(profiles, 1):
                if self._stop.is_set():
                    break
                self._state["current"] = profile.label
                proxy: Proxy | None = None
                if pool.size:
                    assigned = assignments.proxy_for(profile.id, pool)
                    proxy = pool.acquire(assigned.raw if assigned is not None else None)
                    if proxy is None:
                        self._state["failed"] += 1
                        self._state["current"] = None
                        await self.bus.log(
                            None, f"Прогрев {profile.label}: нет свободных прокси", "warn"
                        )
                        continue
                session: BrowserSession | None = None
                try:
                    session = await provider.open(profile, proxy)
                    await self._browse(session, settings)
                    self._state["done"] += 1
                    await self.bus.log(
                        None,
                        f"Прогрев {index}/{len(profiles)}: {profile.label} — готов",
                        "success",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._state["failed"] += 1
                    await self.bus.log(
                        None,
                        f"Прогрев {profile.label}: {type(exc).__name__}: {exc}"[:400],
                        "error",
                    )
                finally:
                    if session is not None and provider is not None:
                        try:
                            await provider.close(session)
                        except Exception:  # noqa: BLE001
                            pass
                    if proxy is not None:
                        pool.release(proxy)
                    self._state["current"] = None
                if self._stop.is_set() or index >= len(profiles):
                    break
                await asyncio.sleep(random.uniform(5, 12))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._state["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self._state["running"] = False
            self._state["current"] = None
            self._state["finished_at"] = datetime.now().strftime("%H:%M:%S")
            self._task = None
            await self.bus.log(
                None,
                f"Прогрев завершён: готово {self._state['done']}, "
                f"ошибок {self._state['failed']}",
                "success" if not self._state["failed"] else "warn",
            )

    async def _browse(self, session: BrowserSession, settings: AppSettings) -> None:
        page = session.page
        if session.hwnd and not settings.browser.headless and not settings.browser.show_on_start:
            # Греем пакетно — окна не должны перекрывать работу оператору.
            win_window.set_visible(session.hwnd, False)
            session.visible = False

        await page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        if await self._blocked(page):
            raise RuntimeError("Ozon блокирует IP: страница «Похоже, нет соединения»")
        await _wait_challenge(page, "прогрев", self.bus, None, session=session)
        await _dismiss_overlays(page)
        await asyncio.sleep(random.uniform(3.0, 6.0))
        await self._scroll(page, random.randint(4, 8))

        query = random.choice(WARM_QUERIES)
        url = f"{BASE}/search/?text={quote(query)}&from_global=true"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if await self._blocked(page):
            raise RuntimeError("Ozon блокирует IP: страница «Похоже, нет соединения»")
        await _wait_challenge(page, "прогрев", self.bus, None, session=session)
        await _dismiss_overlays(page)
        await asyncio.sleep(random.uniform(2.0, 4.0))
        await self._scroll(page, random.randint(3, 6))
        await human.wheel(page, -random.randint(200, 500))

    @staticmethod
    async def _blocked(page: Any) -> bool:
        """Страница-заглушка Ozon «Похоже, нет соединения» = блок по IP."""
        try:
            body = (await page.inner_text("body"))[:1500].lower()
        except Exception:  # noqa: BLE001
            return False
        return (
            "нет соединения" in body
            or "доступ ограничен" in body
            or "обратиться в поддержку" in body
        )

    @staticmethod
    async def _scroll(page: Any, steps: int) -> None:
        for _ in range(steps):
            await human.wheel(page, random.randint(350, 900))
            await asyncio.sleep(random.uniform(0.8, 1.6))
