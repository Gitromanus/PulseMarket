"""Проверка прокси на доступность Ozon: отсеять заблокированные адреса.

Каждый прокси проверяется в отдельном чистом профиле: открываем выдачу Ozon и
смотрим, отдаёт ли площадка страницу-заглушку «Похоже, нет соединения» (блок по
IP/ASN). Проверка идёт в фоне, по одному прокси, и не мешает прогону: запускать
можно только когда очередь и прогрев остановлены.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..browser import win_window
from ..browser.profiles import Profile
from ..browser.providers import BrowserProvider, build_provider
from ..browser.proxies import Proxy, ProxyPool
from ..browser.session import BrowserSession
from ..core.config import AppSettings, app_data_dir, load_settings
from ..core.events import EventBus
from ..flows.ozon import _find_challenge

OZON_CHECK_URL = "https://www.ozon.ru/search/?text=%D0%BB%D0%B0%D0%BC%D0%BF%D0%BE%D1%87%D0%BA%D0%B0&from_global=true"
BLOCK_MARKERS = ("нет соединения", "доступ ограничен", "incident", "обратиться в поддержку")


def _results_path() -> Path:
    return app_data_dir() / "proxy_check.json"


class ProxyCheckManager:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._items: list[dict[str, Any]] = self._load()
        self._state = self._idle_state()
        self._path: str | None = None
        if self._items:
            self._counts()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "running": False,
            "total": 0,
            "checked": 0,
            "ok": 0,
            "blocked": 0,
            "challenge": 0,
            "failed": 0,
            "current": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def status(self) -> dict[str, Any]:
        """Состояние для UI: без логинов/паролей прокси."""
        state = dict(self._state)
        state["items"] = [
            {key: value for key, value in item.items() if key != "raw"}
            for item in self._items
        ]
        return state

    def results_by_raw(self) -> dict[str, str]:
        """Статус последней проверки по каждой строке прокси."""
        return {item["raw"]: str(item.get("status")) for item in self._items}

    def _counts(self) -> None:
        items = self._items
        self._state.update(
            {
                "total": len(items),
                "checked": len(items),
                "ok": sum(1 for item in items if item["status"] == "ok"),
                "blocked": sum(1 for item in items if item["status"] == "blocked"),
                "challenge": sum(1 for item in items if item["status"] == "challenge"),
                "failed": sum(1 for item in items if item["status"] == "error"),
            }
        )

    async def start(self, settings: AppSettings) -> dict[str, Any]:
        if self._state["running"]:
            return {"ok": False, "error": "Проверка уже идёт"}
        pool = ProxyPool(settings.general.proxies_file or None)
        if pool.size == 0:
            return {"ok": False, "error": "Прокси не заданы: укажите файл или добавьте адреса"}
        self._path = settings.general.proxies_file or None
        self._stop.clear()
        self._items = []
        self._state = {
            "running": True,
            "total": pool.size,
            "checked": 0,
            "ok": 0,
            "blocked": 0,
            "challenge": 0,
            "failed": 0,
            "current": None,
            "error": None,
            "started_at": datetime.now().strftime("%H:%M:%S"),
            "finished_at": None,
        }
        self._task = asyncio.create_task(self._run(settings, list(pool.items)), name="proxy-check")
        await self.bus.log(None, f"Проверка прокси: {pool.size} шт. по Ozon")
        return {"ok": True, "total": pool.size, "status": self.status()}

    async def stop(self) -> dict[str, Any]:
        if not self._state["running"]:
            return {"ok": False, "error": "Проверка не запущена"}
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return {"ok": True, "status": self.status()}

    async def prune_blocked(self, include_errors: bool = False) -> dict[str, Any]:
        """Убирает из файла заблокированные (и, по желанию, нерабочие) адреса."""
        if self._state["running"]:
            return {"ok": False, "error": "Сначала дождитесь окончания проверки"}
        drop = {"blocked"} | ({"error"} if include_errors else set())
        blocked = {item["raw"] for item in self._items if item["status"] in drop}
        if not blocked:
            return {
                "ok": False,
                "error": "Нерабочих прокси не найдено"
                if include_errors
                else "Заблокированных прокси не найдено",
            }
        path = Path(self._path or load_settings().general.proxies_file or "")
        if not path.is_file():
            return {"ok": False, "error": "Файл прокси не найден"}
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        kept = [line for line in lines if line.strip() and line.strip() not in blocked]
        if not kept:
            return {
                "ok": False,
                "error": "Все прокси заблокированы — файл оставлен без изменений",
            }
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        removed = len(lines) - len(kept)
        await self.bus.log(
            None,
            f"Из файла прокси удалено заблокированных: {removed} (бэкап {backup.name})",
            "warn",
        )
        self._items = [item for item in self._items if item["raw"] not in blocked]
        self._counts()
        return {"ok": True, "removed": removed, "left": len(kept), "status": self.status()}

    # ---------- проверка ----------

    async def _run(self, settings: AppSettings, proxies: list[Proxy]) -> None:
        provider: BrowserProvider | None = None
        try:
            provider = build_provider(settings.browser)
            for index, proxy in enumerate(proxies, 1):
                if self._stop.is_set():
                    break
                self._state["current"] = proxy.masked()
                item = await self._check_one(provider, proxy, settings)
                self._items.append(item)
                self._counts()
                level = "success" if item["status"] == "ok" else "warn"
                await self.bus.log(
                    None,
                    f"Прокси {index}/{len(proxies)} {proxy.masked()}: {item['status']}"
                    + (f" ({item['error']})" if item.get("error") else ""),
                    level,
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._state["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self._state["running"] = False
            self._state["current"] = None
            self._state["finished_at"] = datetime.now().strftime("%H:%M:%S")
            self._counts()
            self._save()
            self._task = None
            await self.bus.log(
                None,
                f"Проверка прокси завершена: ок {self._state['ok']}, "
                f"блок {self._state['blocked']}, challenge {self._state['challenge']}, "
                f"ошибок {self._state['failed']}",
                "success" if not self._state["blocked"] else "warn",
            )

    async def _check_one(
        self, provider: BrowserProvider, proxy: Proxy, settings: AppSettings
    ) -> dict[str, Any]:
        folder = Path(tempfile.mkdtemp(prefix="pf-proxy-check-")) / "profile"
        profile = Profile(folder=folder, kind="noauth", id="proxy-check")
        session: BrowserSession | None = None
        item: dict[str, Any] = {
            "raw": proxy.raw,
            "server": proxy.server,
            "masked": proxy.masked(),
            "status": "error",
            "http": None,
            "error": None,
            "checked_at": datetime.now().strftime("%H:%M:%S"),
        }
        try:
            session = await provider.open(profile, proxy)
            if session.hwnd and not settings.browser.headless:
                # Проверяем в том же режиме, что и рабочий прогон (headed),
                # но окно прячем, чтобы не мешало оператору.
                win_window.set_visible(session.hwnd, False)
                session.visible = False
            page = session.page

            def apply(status: str, error: str | None = None) -> None:
                item["status"] = status
                item["error"] = error

            response = await page.goto(OZON_CHECK_URL, wait_until="domcontentloaded", timeout=45000)
            item["http"] = response.status if response is not None else None
            await page.wait_for_timeout(3000)
            status, error = await self._classify(page, item["http"])
            # JS-челлендж Ozon часто проходит сам за несколько секунд.
            if status == "challenge":
                for _ in range(10):
                    await page.wait_for_timeout(2000)
                    status, error = await self._classify(page, item["http"])
                    if status != "challenge":
                        break
            # 403/заглушка бывают транзиентными: даём странице второй шанс.
            if status == "blocked":
                try:
                    await page.wait_for_timeout(3000)
                    response = await page.reload(wait_until="domcontentloaded", timeout=45000)
                    item["http"] = response.status if response is not None else item["http"]
                    await page.wait_for_timeout(3000)
                    status, error = await self._classify(page, item["http"])
                except Exception:  # noqa: BLE001 — frame detached/abort: оставляем прежний вердикт
                    pass
            apply(status, error)
        except Exception as exc:  # noqa: BLE001
            item["status"] = "error"
            item["error"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            if session is not None:
                try:
                    await provider.close(session)
                except Exception:  # noqa: BLE001
                    pass
            try:
                shutil.rmtree(folder.parent, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
        return item

    @staticmethod
    async def _classify(page: Any, http: int | None) -> tuple[str, str | None]:
        """HTTP 403 сам по себе не значит блок: у Ozon рабочий DOM бывает с 403."""
        try:
            cards = await page.locator('a[href*="/product/"]').count()
            grid = await page.locator('[data-widget="tileGridDesktop"]').count()
        except Exception:  # noqa: BLE001
            cards = grid = 0
        # Реальная выдача важнее статуса и текста: блок-страница её не содержит.
        if cards or grid:
            return "ok", None
        try:
            body = (await page.inner_text("body"))[:2000].lower()
        except Exception:  # noqa: BLE001
            body = ""
        if any(marker in body for marker in BLOCK_MARKERS):
            return "blocked", "страница-заглушка Ozon"
        try:
            if await _find_challenge(page):
                return "challenge", None
        except Exception:  # noqa: BLE001
            pass
        if http is None:
            return "error", "нет ответа"
        if http in (403, 429):
            return "blocked", f"HTTP {http} без выдачи"
        if http >= 400:
            return "error", f"HTTP {http}"
        return "ok", None

    # ---------- хранение ----------

    @staticmethod
    def _load() -> list[dict[str, Any]]:
        import json

        path = _results_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict) and item.get("raw")]
        except Exception:  # noqa: BLE001
            pass
        return []

    def _save(self) -> None:
        import json

        path = _results_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": self._items,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
