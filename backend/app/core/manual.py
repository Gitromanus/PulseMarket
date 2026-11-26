"""Ручной режим (M6): открыть профиль, оставить браузер оператору, закрыть по команде.

Не пересекается с очередью: задания и lease не трогаются. Профиль, занятый
потоком прогона, открыть нельзя — это проверяется по активным сессиям supervisor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..browser.profiles import Profile, ProfileStore
from ..browser.providers import BrowserProvider, build_provider
from ..browser.proxies import Proxy, parse_proxy
from ..browser.session import BrowserSession
from ..core.config import AppSettings
from ..core.events import EventBus

DEFAULT_URL = "https://www.ozon.ru/cart"


@dataclass
class ManualSession:
    id: str
    session: BrowserSession
    profile: Profile
    proxy: Proxy | None
    provider: BrowserProvider
    created_at: datetime


class ManualManager:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.items: dict[str, ManualSession] = {}

    def _info(self, item: ManualSession) -> dict[str, Any]:
        info = item.session.info()
        return {
            "id": item.id,
            "created_at": item.created_at.strftime("%H:%M:%S"),
            **info,
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._info(item) for item in self.items.values()]

    def busy_paths(self) -> set[str]:
        """Папки, открытые в ручном режиме: их нельзя греть или занимать."""
        return {str(item.profile.folder) for item in self.items.values()}

    def _resolve_profile(
        self, settings: AppSettings, profile_path: str | None, busy_paths: set[str]
    ) -> Profile:
        if profile_path:
            folder = Path(profile_path)
            if not folder.exists():
                raise FileNotFoundError(f"папка профиля не найдена: {folder}")
            if str(folder).lower() in {p.lower() for p in busy_paths}:
                raise RuntimeError("профиль занят потоком прогона")
            return Profile(folder=folder, kind="manual", id=folder.name)
        return ProfileStore(settings).create_ephemeral()

    async def start(
        self,
        settings: AppSettings,
        *,
        profile_path: str | None = None,
        proxy_raw: str | None = None,
        url: str | None = None,
        busy_paths: set[str] | None = None,
    ) -> dict[str, Any]:
        try:
            profile = self._resolve_profile(settings, profile_path, busy_paths or set())
        except (FileNotFoundError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

        proxy = parse_proxy(proxy_raw) if proxy_raw else None
        provider = build_provider(settings.browser)
        try:
            session = await provider.open(profile, proxy)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}

        sid = uuid.uuid4().hex[:8]
        item = ManualSession(
            id=sid,
            session=session,
            profile=profile,
            proxy=proxy,
            provider=provider,
            created_at=datetime.now(),
        )
        self.items[sid] = item

        target = url or DEFAULT_URL
        try:
            await session.page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            await self.bus.log(None, f"Ручной режим: страница не открылась ({exc})", "warn")

        await self.bus.log(
            None,
            f"Ручной режим: профиль {profile.label}, прокси {proxy.masked() if proxy else 'direct'}",
        )
        return {"ok": True, "item": self._info(item)}

    def _get(self, sid: str) -> ManualSession | None:
        return self.items.get(sid)

    async def set_visible(self, sid: str, visible: bool) -> dict[str, Any]:
        item = self._get(sid)
        if item is None:
            return {"ok": False, "error": "Сессия не найдена"}
        changed = item.session.set_visible(visible)
        if not changed:
            return {"ok": False, "error": "Окно недоступно: headless или CDP-провайдер"}
        await self.bus.log(None, f"Ручной режим: окно {item.profile.label} — {'показано' if visible else 'скрыто'}")
        return {"ok": True, "item": self._info(item)}

    async def navigate(self, sid: str, url: str) -> dict[str, Any]:
        item = self._get(sid)
        if item is None:
            return {"ok": False, "error": "Сессия не найдена"}
        try:
            await item.session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        await self.bus.log(None, f"Ручной режим: переход на {url}")
        return {"ok": True, "item": self._info(item)}

    async def close(self, sid: str) -> dict[str, Any]:
        item = self.items.pop(sid, None)
        if item is None:
            return {"ok": False, "error": "Сессия не найдена"}
        try:
            await item.provider.close(item.session)
        except Exception:  # noqa: BLE001
            pass
        await self.bus.log(None, f"Ручной режим: профиль {item.profile.label} закрыт")
        return {"ok": True}

    async def close_all(self) -> None:
        for sid in list(self.items):
            await self.close(sid)
