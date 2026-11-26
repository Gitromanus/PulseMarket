"""Supervisor: N потоков, очередь, watchdog, события для UI.

Сценарий на этом этапе — детерминированная заглушка (M0/M1): она проверяет
захват заданий, lease, отчётность и доставку событий. Реальный браузерный
flow подключается в M2/M3 через app.flows.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any

from .config import AppSettings, load_settings
from .events import EventBus
from .license import require_license
from ..browser import win_window
from ..browser.profiles import ProfileStore
from ..browser.providers import BrowserProvider, ProviderError, build_provider
from ..browser.proxies import ProxyAssignments, ProxyPool
from ..browser.session import BrowserSession
from ..db.database import Database
from ..flows.base import ScenarioError
from ..flows.check import run_check
from ..flows.ozon import run_ozon

# Ошибки сети/прокси на первой навигации: задание не валим, а один раз
# повторяем с другим прокси после короткой паузы.
PROXY_RETRY_MARKERS = ("net::err_failed", "net::err_http_response_code_failure")
PROXY_RETRY_PAUSE = (10.0, 20.0)


def _is_proxy_network_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in PROXY_RETRY_MARKERS)


def _short_error(exc: Exception) -> str:
    return str(exc).splitlines()[0][:160]


PRODUCT_VIEW_STEPS = [
    ("установили геолокацию", 0.4),
    ("открыли главную маркетплейса", 0.6),
    ("ввели поисковый запрос", 0.8),
    ("проскроллили выдачу", 1.0),
    ("открыли карточку конкурента", 0.7),
    ("вернулись в выдачу", 0.5),
    ("открыли целевую карточку", 0.9),
    ("пролистали фото", 0.8),
    ("прочитали описание", 1.0),
    ("проверили отзывы", 1.2),
]


class Supervisor:
    def __init__(self, db: Database, bus: EventBus, sync: Any | None = None) -> None:
        self.db = db
        self.bus = bus
        self.sync = sync
        self.state = "idle"  # idle | running | stopping
        self.threads: dict[int, dict[str, Any]] = {}
        self.stats = {"ok": 0, "errors": 0}
        self._workers: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._watchdog: asyncio.Task | None = None
        self._error_streak = 0
        self._block_streak = 0
        self._profiles: ProfileStore | None = None
        self._proxies: ProxyPool | None = None
        self._assignments = ProxyAssignments()
        self._provider: BrowserProvider | None = None
        self.sessions: dict[int, BrowserSession] = {}
        # Профиль и прокси выдаются на каждый прогон заново (см. _browser_scenario).

    # ---------- публичное состояние ----------

    def snapshot(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        threads: list[dict[str, Any]] = []
        for thread in self.threads.values():
            item = dict(thread)
            session = self.sessions.get(thread["id"])
            if session is not None:
                item["window_visible"] = win_window.is_visible(session.hwnd)
            threads.append(item)
        return {
            "state": self.state,
            "threads": threads,
            "stats": dict(self.stats),
        }

    async def metrics(self) -> dict[str, Any]:
        counts = await self.db.counts()
        counts.update(
            {
                "threads_active": sum(
                    1 for t in self.threads.values() if t["status"] == "running"
                ),
                "run_state": self.state,
                "ok": self.stats["ok"],
                "errors": self.stats["errors"],
            }
        )
        return counts

    # ---------- управление ----------

    async def start(self, threads_override: int | None = None) -> dict[str, Any]:
        if self.state == "running":
            return {"ok": False, "error": "Прогон уже запущен"}

        settings = load_settings()
        license_error = require_license()
        if license_error:
            await self.bus.log(None, license_error, "error")
            return {"ok": False, "error": license_error}
        if self.sync is not None and settings.general.sync_enabled:
            await self.sync.cycle()  # подтянуть свежие строки до старта потоков
        threads = threads_override or settings.general.threads
        pending = await self.db.count_pending()
        if pending == 0:
            counts = await self.db.counts()
            finished = int(counts.get("done", 0)) + int(counts.get("errors", 0))
            if finished:
                message = (
                    f"Очередь пуста: готово {counts.get('done', 0)}, "
                    f"ошибок {counts.get('errors', 0)}, в ожидании 0. "
                    "Очистка таблицы не сбрасывает очередь в базе — нажмите "
                    "«↻ Повторить выполненные», чтобы прогнать их снова."
                )
            else:
                message = (
                    "Очередь пуста: добавьте задания в лист, загрузите демо-данные "
                    "или дождитесь синхронизации"
                )
            return {"ok": False, "error": message}

        self.state = "running"
        self.stats = {"ok": 0, "errors": 0}
        self._error_streak = 0
        self._block_streak = 0
        self._stop.clear()
        self.sessions.clear()

        if settings.browser.enabled:
            # Пулы переживают остановку прогона: круговой обход продолжается
            # со следующего профиля/прокси, а не начинается заново.
            if self._profiles is None:
                self._profiles = ProfileStore(settings)
            else:
                self._profiles.refresh(settings)
            proxies_path = settings.general.proxies_file or None
            if self._proxies is None or self._proxies.path != proxies_path:
                self._proxies = ProxyPool(proxies_path)
            self._provider = build_provider(settings.browser)
            profile_state = self._profiles.state()
            await self.bus.log(
                None,
                f"Браузер: {settings.browser.provider}, активных профилей {profile_state['active']}, "
                f"прокси {self._proxies.size}",
            )
            # При 100% переиспользовании потоков не может быть больше, чем
            # профилей: иначе второй поток ждёт профиль и срывает задания.
            usable = max(1, int(profile_state["active"]))
            if settings.view.profile_use_pct >= 100 and threads > usable:
                await self.bus.log(
                    None,
                    f"Потоков {threads} → {usable}: при 100% переиспользовании доступно "
                    f"профилей {usable}. Добавьте профили или снизьте переиспользование.",
                    "warn",
                )
                threads = usable
            from ..browser.providers import engine_name
            from .config import system_timezone

            tz = settings.browser.timezone or system_timezone()
            tz_note = "из настроек" if settings.browser.timezone else "системный"
            channel = settings.browser.channel
            await self.bus.log(
                None,
                f"Движок: {engine_name()}, браузер: {channel}, часовой пояс: {tz} ({tz_note})",
            )
            if self._proxies.size == 0:
                await self.bus.log(
                    None,
                    "Прокси не заданы: Ozon с текущего IP обычно отдаёт антибот. "
                    "Добавьте residential/mobile-прокси в «Настройки → Основные».",
                    "warn",
                )
            if settings.browser.headless:
                await self.bus.log(
                    None,
                    "Headless-режим заметен антиботу: для Ozon лучше окно (headless = Нет)",
                    "warn",
                )
        else:
            self._profiles = None
            self._proxies = None
            self._provider = None

        self.threads = {
            i: {"id": i, "status": "idle", "task_id": None, "browser_visible": False}
            for i in range(1, threads + 1)
        }
        await self.bus.publish("run_state", {"state": self.state, "threads": threads})
        await self.bus.log(None, f"Запуск прогона: потоков {threads}, заданий в очереди {pending}")

        self._workers = [
            asyncio.create_task(self._worker(i, settings), name=f"worker-{i}")
            for i in range(1, threads + 1)
        ]
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="watchdog")
        return {"ok": True, "threads": threads, "pending": pending}

    async def stop(self) -> dict[str, Any]:
        if self.state != "running":
            return {"ok": False, "error": "Прогон не запущен"}
        self.state = "stopping"
        self._stop.set()
        await self.bus.publish("run_state", {"state": self.state, "threads": len(self.threads)})
        await self.bus.log(None, "Останавливаем потоки (ждём завершения текущего шага)...")

        if self._watchdog:
            self._watchdog.cancel()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._watchdog = None

        # Страховка: если поток отменили в момент await, окно могло остаться.
        if self._provider is not None:
            for session in list(self.sessions.values()):
                try:
                    await asyncio.wait_for(self._provider.close(session), timeout=8)
                except Exception:  # noqa: BLE001
                    pass
        self.sessions.clear()

        await self._finish_run("остановлено оператором")
        return {"ok": True}

    async def restart(self, threads_override: int | None = None) -> dict[str, Any]:
        if self.state == "running":
            await self.stop()
            await asyncio.sleep(0.3)
        return await self.start(threads_override)

    def busy_profile_paths(self) -> set[str]:
        """Папки профилей, занятые потоками: ручной режим не должен их открывать."""
        return {str(session.profile.folder) for session in self.sessions.values()}

    async def set_browser_visible(self, thread_id: int, visible: bool) -> dict[str, Any]:
        thread = self.threads.get(thread_id)
        if not thread:
            return {"ok": False, "error": f"Поток №{thread_id} не найден"}
        session = self.sessions.get(thread_id)
        if session is None:
            return {"ok": False, "error": "У потока нет открытого браузера"}

        changed = session.set_visible(visible)
        thread["browser_visible"] = session.visible
        await self._publish_thread(thread_id)
        if not changed:
            return {
                "ok": False,
                "error": "Окно не найдено: headless-режим или CDP-провайдер управляет окном сам",
            }
        return {"ok": True, "thread_id": thread_id, "visible": session.visible}

    # ---------- воркер ----------

    async def _worker(self, thread_id: int, settings: AppSettings) -> None:
        state = self.threads[thread_id]
        state["status"] = "waiting"
        await self._publish_thread(thread_id)

        while not self._stop.is_set():
            lease_minutes = max(30, int(settings.general.repeat_cooldown_min) + 15)
            task = await self.db.claim_task(f"thread-{thread_id}", lease_minutes=lease_minutes)
            if task is None:
                if await self.db.count_unfinished() == 0:
                    await self.bus.log(thread_id, "Очередь пуста, поток завершает работу")
                    break
                if state["status"] != "waiting":
                    state["status"] = "waiting"
                    await self._publish_thread(thread_id)
                await asyncio.sleep(2)
                continue

            # Пауза повтора — на уровне задания, а не потока: иначе второй
            # поток сразу подхватит ту же строку и обойдёт кулдаун.
            cooldown = settings.general.repeat_cooldown_min * 60
            age = await self.db.last_run_age_minutes(task["id"])
            if cooldown > 0 and age is not None and age * 60 < cooldown:
                delay = cooldown - age * 60
                await self.bus.log(
                    thread_id,
                    f"Повтор строки {task['sheet_row']}: пауза {delay / 60:.1f} мин, "
                    "чтобы один и тот же товар не шёл подряд",
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    await self.db.release_task(task["id"])
                    raise

            state.update(
                {
                    "task_id": task["id"],
                    "status": "running",
                    "sku": task["sku"],
                    "profile": None,
                    "proxy": None,
                    "hwnd": None,
                }
            )
            await self._publish_thread(thread_id)
            await self.bus.log(
                thread_id,
                f"Взяли строку {task['sheet_row']} в работу: {task['sku']} / «{task['query']}»",
            )
            await self.bus.publish("task_update", {"task_id": task["id"], "status": "claimed"})

            started = None  # отметку ставит SQLite в локальном часовом поясе
            steps: list[dict[str, Any]] = []
            try:
                if settings.browser.enabled and self._provider is not None:
                    position, cart, liked = await self._browser_scenario(
                        thread_id, task, settings, steps, state
                    )
                else:
                    position, cart, liked = await self._scenario(thread_id, task, settings, steps)
                updated = await self.db.complete_task(task["id"], position, cart, liked)
                self.stats["ok"] += 1
                self._error_streak = 0
                self._block_streak = 0

                await self.db.record_run(
                    task_id=task["id"],
                    thread_id=thread_id,
                    result="ok",
                    position=position,
                    cart_added=cart,
                    liked=liked,
                    steps=steps,
                    started_at=started,
                )
                await self.bus.log(
                    thread_id,
                    f"Готово: позиция {position if position is not None else '—'}, "
                    f"корзина +{cart}, лайки +{liked}",
                    "success",
                )
                if updated:
                    await self.bus.publish(
                        "task_update",
                        {
                            "task_id": task["id"],
                            "status": updated["status"],
                            "done_runs": updated["done_runs"],
                            "target_runs": updated["target_runs"],
                        },
                    )
                if self.sync is not None:
                    if cart:
                        await self.sync.append_order(task, position)
                    await self.sync.push_now()
            except asyncio.CancelledError:
                await self.db.release_task(task["id"])
                state["task_id"] = None
                await self._publish_thread(thread_id)
                raise
            except Exception as exc:  # noqa: BLE001 — любая ошибка сценария не должна ронять прогон
                self.stats["errors"] += 1
                self._error_streak += 1
                text = str(exc).lower()
                await self.db.fail_task(task["id"], str(exc)[:300])
                if "product_unavailable" in text:
                    # Товар снят/закончился: повторять 10 раз бессмысленно.
                    await self.db.mark_error(task["id"], str(exc)[:300])
                    await self.bus.log(
                        thread_id,
                        "Товар недоступен (закончился/снят с продажи): строка помечена "
                        "ошибкой, повторы отключены",
                        "warn",
                    )
                await self.db.record_run(
                    task_id=task["id"],
                    thread_id=thread_id,
                    result="error",
                    error_code=type(exc).__name__,
                    error_msg=str(exc)[:500],
                    steps=steps,
                    started_at=started,
                )
                await self.bus.log(thread_id, f"Ошибка: {exc}", "error")
                if "ozon_blocked" in text or "антибот" in text:
                    self._block_streak += 1
                    if self._block_streak >= 2:
                        await self.bus.log(
                            None,
                            "Ozon блокирует запросы (антибот). Прогон остановлен: подождите "
                            "10–15 минут, проверьте прокси и снизьте число потоков",
                            "error",
                        )
                        asyncio.create_task(self.stop())
                        return
                    await self.bus.log(
                        thread_id,
                        "Антибот: пауза 15–25 секунд перед повтором, чтобы не усугублять блок",
                        "warn",
                    )
                    await asyncio.sleep(random.uniform(15, 25))
                else:
                    self._block_streak = 0
                if settings.general.error_limit and self._error_streak >= settings.general.error_limit:
                    await self.bus.log(
                        None,
                        f"Достигнут лимит ошибок ({settings.general.error_limit}), останавливаем прогон",
                        "error",
                    )
                    asyncio.create_task(self.stop())
                    return
                await asyncio.sleep(1)

            state["task_id"] = None
            await self._publish_thread(thread_id)

            # Не открываем следующую карточку в ту же секунду: у человека
            # между товарами есть пауза, а мгновенный рестарт — явный паттерн.
            if not self._stop.is_set() and await self.db.count_pending() > 0:
                gap = random.uniform(20, 70)
                await self.bus.log(thread_id, f"Пауза между карточками {gap:.0f} с")
                await asyncio.sleep(gap)

        state["status"] = "idle"
        await self._publish_thread(thread_id)

    async def _browser_scenario(
        self,
        thread_id: int,
        task: dict[str, Any],
        settings: AppSettings,
        steps: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> tuple[int | None, int, int]:
        """Реальный браузер: Ozon-сценарий для маркетплейса, иначе проверочная страница."""
        assert self._profiles is not None and self._proxies is not None

        profile = None
        allow_ephemeral = settings.view.profile_use_pct < 100
        for attempt in range(30):
            profile, _ = self._profiles.acquire(
                settings.view.profile_use_pct, allow_ephemeral=allow_ephemeral
            )
            if profile is not None:
                break
            if attempt == 0:
                await self.bus.log(
                    thread_id,
                    "Ждём освобождения прогретого профиля (100% переиспользование)",
                    "warn",
                )
            await asyncio.sleep(1)
        if profile is None:
            raise RuntimeError("нет свободного прогретого профиля: уменьшите число потоков")

        try:
            for attempt in (1, 2):
                try:
                    return await self._run_browser_once(
                        thread_id, task, settings, steps, state, profile
                    )
                except Exception as exc:  # noqa: BLE001
                    if attempt >= 2 or not _is_proxy_network_error(str(exc)):
                        raise
                    # Профиль остаётся тем же, меняем только закреплённый прокси.
                    failed_raw = self._assignments.get(profile.id)
                    new_proxy = self._assignments.reassign(
                        profile.id,
                        self._proxies,
                        avoid={failed_raw} if failed_raw else set(),
                    )
                    changed = bool(new_proxy and new_proxy.raw != failed_raw)
                    pause = random.uniform(*PROXY_RETRY_PAUSE)
                    await self.bus.log(
                        thread_id,
                        f"Сетевая ошибка прокси ({_short_error(exc)}). "
                        f"Повторяем через {pause:.0f} с "
                        + ("с другим прокси" if changed else "с тем же прокси (других нет)"),
                        "warn",
                    )
                    await asyncio.sleep(pause)
        finally:
            self._profiles.release(profile)
            state.update({"profile": None, "proxy": None, "hwnd": None, "browser_visible": False})
            await self._publish_thread(thread_id)

    async def _run_browser_once(
        self,
        thread_id: int,
        task: dict[str, Any],
        settings: AppSettings,
        steps: list[dict[str, Any]],
        state: dict[str, Any],
        profile: Any,
    ) -> tuple[int | None, int, int]:
        """Один заход в браузер с закреплённым за профилем прокси."""
        assert self._proxies is not None and self._provider is not None

        # Прокси закреплён за профилем: новый прогон = новый профиль со своим
        # постоянным IP, а не тот же профиль с прыгающим адресом.
        proxy = None
        assigned = None
        if self._proxies.size:
            assigned = self._assignments.proxy_for(profile.id, self._proxies)
            preferred = assigned.raw if assigned is not None else None
            proxy = self._proxies.acquire(preferred)
        if self._proxies.size and proxy is None:
            # Нельзя молча уходить в direct: это другой IP и мгновенный антибот.
            await self.bus.log(
                thread_id,
                "Прокси профиля занят другим потоком, ждём освобождения",
                "warn",
            )
            preferred = assigned.raw if assigned is not None else None
            for _ in range(60):
                await asyncio.sleep(1)
                proxy = self._proxies.acquire(preferred)
                if proxy is not None:
                    break
            if proxy is None:
                raise RuntimeError(
                    "нет свободных прокси: уменьшите число потоков или расширьте пул"
                )
        state.update(
            {
                "profile": profile.label,
                "profile_kind": profile.kind,
                "proxy": proxy.masked() if proxy else "direct",
            }
        )
        await self._publish_thread(thread_id)
        await self.bus.log(
            thread_id,
            f"Профиль {profile.label} (новый прогон), "
            f"прокси {proxy.masked() if proxy else 'direct'} (закреплён за профилем)",
        )

        session: BrowserSession | None = None
        try:
            session = await self._provider.open(profile, proxy)
            self.sessions[thread_id] = session
            state.update({"hwnd": session.hwnd, "browser_visible": session.visible})
            if session.hwnd and not settings.browser.headless:
                # По умолчанию окно скрыто: оператор видит живое превью и
                # открывает браузер кликом, когда нужно.
                show = bool(settings.browser.show_on_start)
                win_window.set_visible(session.hwnd, show)
                session.visible = show and win_window.is_visible(session.hwnd)
                state["browser_visible"] = session.visible
            await self._publish_thread(thread_id)
            await self.bus.log(
                thread_id,
                f"Браузер запущен (pid {session.pid or '—'}, окно {session.hwnd or '—'})",
            )
            if session.hwnd and not settings.browser.headless and not session.visible:
                await self.bus.log(thread_id, "Окно скрыто: смотрите превью, откроется по клику")
            if session.fingerprint:
                await self.bus.log(thread_id, f"Отпечаток: {session.fingerprint}")

            if str(task.get("marketplace") or "").lower() == "ozon" and task.get("query"):
                try:
                    result = await run_ozon(session, task, settings, self.bus, thread_id)
                except ScenarioError as exc:
                    location = f" · артефакты: {exc.artifact}" if exc.artifact else ""
                    raise RuntimeError(f"{exc}{location}") from exc
                steps.extend(result.get("steps", []))
                return (
                    result.get("position"),
                    int(result.get("cart", 0)),
                    int(result.get("liked", 0)),
                )

            result = await run_check(
                session, settings.browser.check_url, self.bus, thread_id, settings
            )
            steps.append(
                {
                    "step": "browser_check",
                    "at": datetime.now().strftime("%H:%M:%S"),
                    **{k: result.get(k) for k in ("title", "url", "status", "scrolled")},
                }
            )
            await self.bus.log(
                thread_id,
                f"Браузерный прогон ок: {result.get('title')} (HTTP {result.get('status')})",
                "success",
            )
            return None, 0, 0
        except ProviderError as exc:
            raise RuntimeError(f"браузер: {exc}") from exc
        finally:
            if session is not None and self._provider is not None:
                try:
                    await self._provider.close(session)
                except asyncio.CancelledError:
                    pass
            self.sessions.pop(thread_id, None)
            if proxy is not None:
                self._proxies.release(proxy)
            state.update({"profile": None, "proxy": None, "hwnd": None, "browser_visible": False})
            await self._publish_thread(thread_id)

    async def _scenario(
        self,
        thread_id: int,
        task: dict[str, Any],
        settings: AppSettings,
        steps: list[dict[str, Any]],
    ) -> tuple[int, int, int]:
        """Заглушка сценария. Реальные шаги — app.flows.ozon (M3)."""
        view = settings.view
        for name, weight in PRODUCT_VIEW_STEPS:
            if self._stop.is_set():
                raise asyncio.CancelledError()
            delay = random.uniform(0.4, 1.4) * weight
            await asyncio.sleep(delay)
            steps.append(
                {
                    "step": name,
                    "at": datetime.now().strftime("%H:%M:%S"),
                    "delay": round(delay, 2),
                }
            )
            await self.bus.log(thread_id, name)

        cart = 1 if random.randint(1, 100) <= view.cart_pct else 0
        liked = 1 if random.randint(1, 100) <= view.like_pct else 0
        position = random.randint(3, 74)
        if cart:
            await self.bus.log(thread_id, "Товар добавлен в корзину (профиль → папка выкупов)")
        if liked:
            await self.bus.log(thread_id, "Поставлен лайк товару")
        return position, cart, liked

    # ---------- служебное ----------

    async def _publish_thread(self, thread_id: int) -> None:
        await self.bus.publish("thread_state", self.threads[thread_id])

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            released = await self.db.release_expired()
            if released:
                await self.bus.log(None, f"Watchdog: возвращено в очередь заданий — {released}", "warn")
            if self.state == "running" and all(
                t["status"] in ("idle", "done") for t in self.threads.values()
            ):
                await self._finish_run("все потоки завершили работу")
                return

    async def _finish_run(self, reason: str) -> None:
        if self.state == "idle":
            return
        self.state = "idle"
        self._stop.set()
        await self.bus.publish(
            "run_state",
            {"state": self.state, "threads": len(self.threads), "reason": reason},
        )
        await self.bus.log(None, f"Прогон завершён: {reason}. Успешно {self.stats['ok']}, ошибок {self.stats['errors']}")
        self.threads.clear()
