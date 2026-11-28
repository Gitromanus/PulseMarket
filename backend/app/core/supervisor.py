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
from ..integrations.ltespace import (
    LtespaceError,
    auto_rotation_snapshot,
    proxy_exit_ips,
    rotate_all,
    rotate_for_proxy,
    sync_proxies,
)

# Ошибки сети/прокси на первой навигации: задание не валим, а один раз
# повторяем с другим прокси после короткой паузы.
PROXY_RETRY_MARKERS = ("net::err_failed", "net::err_http_response_code_failure")
PROXY_RETRY_PAUSE = (10.0, 20.0)
# 407 приходит, когда Chrome не приложил логин/пароль прокси (гонка на первом
# запросе после старта или устаревшие доступы LTESpace).
PROXY_AUTH_MARKERS = ("407", "err_proxy_auth", "err_invalid_auth_credentials")


def _is_proxy_network_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in PROXY_RETRY_MARKERS)


def _is_proxy_auth_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in PROXY_AUTH_MARKERS)


async def _proxy_exit_ip(session: Any) -> str | None:
    """Внешний IP сессии: запрос идёт через тот же прокси, что и браузер.

    ``context.request`` использует прокси контекста, поэтому это реальный
    адрес, с которого Ozon видит поток, — по нему и видна ротация.
    """
    try:
        response = await session.context.request.get(
            "https://api.ipify.org?format=json", timeout=10000
        )
        if not response.ok:
            return None
        data = await response.json()
    except Exception:  # noqa: BLE001 — определение IP не должно ломать сценарий
        return None
    ip = data.get("ip") if isinstance(data, dict) else None
    return str(ip) if ip else None


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
        self._captcha_restarts = 0
        self._captcha_restarting = False
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
        progress = await self.db.progress()
        counts.update(
            {
                "runs_done": progress["done"],
                "runs_target": progress["target"],
                "progress_percent": progress["percent"],
                "progress_tasks": progress["tasks"],
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

    async def start(
        self,
        threads_override: int | None = None,
        reset_captcha: bool = True,
        allowed_ports: set[int] | None = None,
    ) -> dict[str, Any]:
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
        if reset_captcha:
            # Ручной запуск даёт свежий лимит автоперезапусков по капче,
            # а внутренний перезапуск — нет (иначе цикл бесконечный).
            self._captcha_restarts = 0
        self._stop.clear()
        self.sessions.clear()

        if settings.browser.enabled:
            # Свежие прокси LTESpace подтягиваем до создания пула: иначе поток
            # возьмёт устаревший список из файла (истёкшие/заменённые каналы).
            if settings.ltespace.enabled and settings.ltespace.sync_proxies:
                try:
                    result = await asyncio.to_thread(sync_proxies, settings)
                    await self.bus.log(
                        None,
                        f"LTESpace: прокси обновлены — {result['written']} в пуле, "
                        f"пропущено {result['skipped']}",
                    )
                    if result.get("settings_changed"):
                        settings = load_settings()
                except LtespaceError as exc:
                    await self.bus.log(
                        None, f"LTESpace: прокси не обновлены — {exc}", "warn"
                    )
            # Каждый прогон — новый IP: на приватных каналах дёргаем ссылку
            # ротации до открытия браузеров.
            if (
                settings.ltespace.enabled
                and settings.ltespace.rotate_before_run
                and settings.ltespace.api_key
            ):
                try:
                    rotated = await asyncio.to_thread(rotate_all, settings)
                    ips = ", ".join(
                        f"{port}: {ip}"
                        for port, ip in rotated.get("new_ips", {}).items()
                    )
                    await self.bus.log(
                        None,
                        f"LTESpace: IP сменён перед прогоном — "
                        f"{len(rotated['rotated'])} шт., без ручной ротации "
                        f"{len(rotated['skipped'])}"
                        + (f" · {ips}" if ips else ""),
                    )
                except LtespaceError as exc:
                    await self.bus.log(
                        None, f"LTESpace: смена IP перед прогоном не удалась — {exc}", "warn"
                    )
            # Пулы переживают остановку прогона: круговой обход продолжается
            # со следующего профиля/прокси, а не начинается заново.
            if self._profiles is None:
                self._profiles = ProfileStore(settings)
            else:
                self._profiles.refresh(settings)
            proxies_path = settings.general.proxies_file or None
            if self._proxies is None or self._proxies.path != proxies_path:
                self._proxies = ProxyPool(proxies_path)
            else:
                # Файл мог обновиться (LTESpace), а прошлый прогон мог быть
                # ограничен только свежими IP — начинаем с полного пула.
                self._proxies.reload()
            self._proxies.max_per_proxy = max(1, int(settings.general.proxy_max_threads))
            if allowed_ports is not None:
                kept = self._proxies.keep_ports(allowed_ports)
                if kept:
                    await self.bus.log(
                        None,
                        f"Прокси: продолжаем только на свежих IP — {kept} шт. "
                        f"(порты {', '.join(map(str, sorted(allowed_ports)))})",
                    )
                    capacity = kept * max(1, int(settings.general.proxy_max_threads))
                    if threads > capacity:
                        await self.bus.log(
                            None,
                            f"Потоков {threads} → {capacity}: свежих прокси {kept}",
                        )
                        threads = max(1, capacity)
                else:
                    # Свежих портов в файле не осталось — не рискуем пустым
                    # пулом, работаем как обычно.
                    self._proxies.reload()
                    await self.bus.log(
                        None,
                        "Прокси: свежих IP в пуле не осталось — работаем со всем пулом",
                        "warn",
                    )
            self._proxies.clear_leases()
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

        if pending < threads:
            await self.bus.log(
                None,
                f"Потоков {threads}, а заданий в очереди {pending}: одновременно "
                f"смогут работать не более {pending} потоков. Повторы одной строки "
                "идут последовательно — добавьте строки в таблицу.",
                "warn",
            )

        self.threads = {
            i: {"id": i, "status": "idle", "task_id": None, "browser_visible": False}
            for i in range(1, threads + 1)
        }
        await self.bus.publish("run_state", {"state": self.state, "threads": threads})
        await self.bus.log(None, f"Запуск прогона: потоков {threads}, заданий в очереди {pending}")

        self._workers = [
            asyncio.create_task(
                self._worker(
                    i,
                    settings,
                    start_delay=(i - 1) * settings.general.thread_start_interval_sec,
                ),
                name=f"worker-{i}",
            )
            for i in range(1, threads + 1)
        ]
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="watchdog")
        return {"ok": True, "threads": threads, "pending": pending}

    async def stop(self, reason: str = "остановлено оператором") -> dict[str, Any]:
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

        await self._finish_run(reason)
        return {"ok": True}

    async def restart(self, threads_override: int | None = None) -> dict[str, Any]:
        if self.state == "running":
            await self.stop()
            await asyncio.sleep(0.3)
        return await self.start(threads_override)

    async def _wait_for_auto_rotation(self, settings: AppSettings) -> set[int]:
        """Ждёт, пока shared-прокси сам сменит IP по таймеру.

        Возвращает порты прокси с новым IP — как только сменился хотя бы
        один канал, прогон можно продолжать на нём, не дожидаясь остальных.
        Пустое множество — IP не сменился (или ожидание отменили).
        """
        try:
            snapshot = await asyncio.to_thread(auto_rotation_snapshot, settings)
            baseline = await asyncio.to_thread(proxy_exit_ips, settings)
        except Exception as exc:  # noqa: BLE001 — ожидание не должно ронять ядро
            await self.bus.log(
                None,
                f"Капча: не удалось узнать IP прокси — {type(exc).__name__}: {exc}",
                "warn",
            )
            return set()
        if not baseline:
            # Прокси мобильные, первый запрос иногда не проходит с первого раза.
            for _ in range(2):
                await asyncio.sleep(5)
                try:
                    baseline = await asyncio.to_thread(proxy_exit_ips, settings)
                except Exception:  # noqa: BLE001
                    baseline = {}
                if baseline:
                    break
        if not baseline:
            await self.bus.log(
                None,
                "Капча: не удалось получить реальный IP через прокси — прогон остановлен",
                "error",
            )
            return set()

        interval = max((int(info["rotation"]) for info in snapshot.values()), default=5)
        # Чуть больше интервала ротации на случай задержки у провайдера,
        # но не дольше 20 минут, чтобы не висеть бесконечно.
        limit_sec = max(300, min(interval * 60 + 150, 1200))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit_sec
        current_note = ", ".join(f"порт {port}: {ip}" for port, ip in baseline.items())
        await self.bus.log(
            None,
            f"Капча: ручной ротации нет (shared). Текущие IP — {current_note}. "
            f"Ждём автосмену раз в {interval} мин, проверяем реальный IP каждые 20 с — "
            f"как только сменится хотя бы один канал, продолжим на нём "
            f"(максимум {round(limit_sec / 60)} мин)",
            "warn",
        )
        next_note = loop.time() + 120
        while True:
            await asyncio.sleep(20)
            if self.state == "running":
                await self.bus.log(
                    None,
                    "Капча: прогон запущен вручную — ожидание смены IP отменено",
                )
                return set()
            now = loop.time()
            if now >= deadline:
                await self.bus.log(
                    None,
                    f"Капча: IP не сменился за {round(limit_sec / 60)} мин — "
                    "прогон остановлен",
                    "error",
                )
                return set()
            try:
                current = await asyncio.to_thread(proxy_exit_ips, settings)
            except Exception:  # noqa: BLE001 — одна неудачная проверка не критична
                current = {}
            changed = {
                port
                for port, old_ip in baseline.items()
                if (new_ip := current.get(port)) and new_ip != old_ip
            }
            if changed:
                for port in sorted(changed):
                    await self.bus.log(
                        None,
                        f"Капча: IP прокси {port} сменился "
                        f"{baseline[port]} → {current[port]}",
                        "success",
                    )
                if len(changed) < len(baseline):
                    await self.bus.log(
                        None,
                        f"Капча: свежих прокси {len(changed)} из {len(baseline)} — "
                        "продолжаем на них, остальные каналы в этом прогоне не используем",
                    )
                return changed
            if now >= next_note:
                left = max(1, round((deadline - now) / 60))
                await self.bus.log(
                    None, f"Капча: IP ещё не сменился, ждём… (осталось ~{left} мин)"
                )
                next_note = now + 120

    async def _restart_after_captcha(self, settings: AppSettings) -> None:
        """Капча Ozon: глушим прогон, меняем IP и стартуем заново.

        Ограничение повторов защищает от бесконечного цикла, если капча
        висит на всех каналах: после ``captcha_restart_limit`` автоперезапуск
        выключается, прогон остаётся остановленным.
        """
        if self._captcha_restarting:
            return
        self._captcha_restarting = True
        try:
            self._captcha_restarts += 1
            limit = settings.ltespace.captcha_restart_limit
            if limit and self._captcha_restarts > limit:
                await self.bus.log(
                    None,
                    f"Капча повторилась {limit} раз — автоперезапуск остановлен, "
                    "проверьте прокси вручную",
                    "error",
                )
                if self.state == "running":
                    await self.stop(reason="капча Ozon — лимит автоперезапусков")
                return

            if self.state == "running":
                await self.stop(reason="капча Ozon — ждём смены IP")
                # Даём закрыться браузерам и watchdog, прежде чем стартовать.
                await asyncio.sleep(0.5)

            rotated: dict[str, Any] = {"rotated": [], "skipped": [], "errors": {}, "new_ips": {}}
            allowed_ports: set[int] | None = None
            try:
                rotated = await asyncio.to_thread(rotate_all, settings)
                await self.bus.log(
                    None,
                    f"Капча: IP сменён у {len(rotated['rotated'])} прокси"
                    + (
                        f", без ручной ротации {len(rotated['skipped'])}"
                        if rotated["skipped"]
                        else ""
                    )
                    + (
                        f", ошибок {len(rotated['errors'])}"
                        if rotated["errors"]
                        else ""
                    )
                    + (
                        " · "
                        + ", ".join(
                            f"{port}: {ip}"
                            for port, ip in rotated.get("new_ips", {}).items()
                        )
                        if rotated.get("new_ips")
                        else ""
                    ),
                    "warn",
                )
            except LtespaceError as exc:
                await self.bus.log(
                    None, f"Капча: сменить IP не удалось — {exc}", "warn"
                )
            except Exception as exc:  # noqa: BLE001 — ротация не должна ломать перезапуск
                await self.bus.log(
                    None, f"Капча: ошибка смены IP — {type(exc).__name__}: {exc}", "warn"
                )

            if settings.ltespace.enabled and not rotated["rotated"] and rotated["skipped"]:
                # Shared-тариф: ручной ротации нет, но IP меняется по таймеру.
                # Не останавливаем прогон навсегда — ждём смены IP и стартуем.
                changed = await self._wait_for_auto_rotation(settings)
                if not changed:
                    return
                allowed_ports = changed

            delay = settings.ltespace.captcha_restart_delay_sec
            if delay:
                await self.bus.log(None, f"Перезапуск прогона через {delay} с")
                await asyncio.sleep(delay)

            if self.state != "running" and await self.db.count_unfinished() > 0:
                result = await self.start(reset_captcha=False, allowed_ports=allowed_ports)
                if not result.get("ok"):
                    await self.bus.log(
                        None,
                        f"Капча: перезапуск не удался — {result.get('error')}",
                        "error",
                    )
            else:
                await self.bus.log(None, "Перезапуск не нужен: очередь пуста")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл прогона не должен падать
            await self.bus.log(
                None,
                f"Автоперезапуск после капчи не удался: {type(exc).__name__}: {exc}",
                "error",
            )
        finally:
            self._captcha_restarting = False

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

    async def _worker(
        self, thread_id: int, settings: AppSettings, start_delay: float = 0.0
    ) -> None:
        state = self.threads[thread_id]
        state["status"] = "waiting"
        await self._publish_thread(thread_id)
        if start_delay > 0:
            await self.bus.log(
                thread_id, f"Старт потока через {start_delay:.0f} с (интервал запуска)"
            )
            await asyncio.sleep(start_delay)

        waiting_logged = False
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
                if not waiting_logged:
                    await self.bus.log(
                        thread_id,
                        "Свободных строк нет — поток простаивает, пока другой поток "
                        "не вернёт строку в очередь (это не блокировка)",
                        "warn",
                    )
                    waiting_logged = True
                await asyncio.sleep(2)
                continue
            waiting_logged = False

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
                    "query": task["query"],
                    "profile": None,
                    "proxy": None,
                    "proxy_ip": None,
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
                self._captcha_restarts = 0

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
                if (
                    "ozon_captcha" in text
                    and settings.ltespace.enabled
                    and settings.ltespace.restart_on_captcha
                ):
                    # Капча — не ошибка задания: после смены IP его нужно
                    # повторить, иначе очередь «пустеет» и прогон встаёт.
                    await self.db.release_after_captcha(task["id"])
                    await self.bus.log(
                        thread_id,
                        "Ozon показал капчу: закрываем браузеры, меняем IP и "
                        "запускаем прогон заново",
                        "warn",
                    )
                    asyncio.create_task(
                        self._restart_after_captcha(settings), name="captcha-restart"
                    )
                    return
                if "ozon_blocked" in text or "антибот" in text:
                    self._block_streak += 1
                    if settings.ltespace.enabled and settings.ltespace.rotate_on_block:
                        session = self.sessions.get(thread_id)
                        proxy = getattr(session, "proxy", None)
                        try:
                            rotated = await asyncio.to_thread(
                                rotate_for_proxy, settings, proxy
                            )
                        except LtespaceError as exc:
                            rotated = {"ok": False, "error": str(exc)}
                        if rotated.get("ok"):
                            ip_note = ""
                            if rotated.get("new_ip"):
                                ip_note = (
                                    f": {rotated.get('old_ip') or '?'} → "
                                    f"{rotated['new_ip']}"
                                )
                            await self.bus.log(
                                thread_id,
                                f"LTESpace: IP сменён (порт {rotated.get('port')}){ip_note}",
                            )
                            session = self.sessions.get(thread_id)
                            live_ip = (
                                await _proxy_exit_ip(session) if session else None
                            )
                            if live_ip:
                                state["proxy_ip"] = live_ip
                                await self._publish_thread(thread_id)
                                await self.bus.log(
                                    thread_id, f"Текущий внешний IP: {live_ip}"
                                )
                        else:
                            await self.bus.log(
                                thread_id,
                                f"LTESpace: IP не сменён — {rotated.get('error')}",
                                "warn",
                            )
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
                gap = random.uniform(
                    settings.general.task_gap_min_sec,
                    settings.general.task_gap_max_sec,
                )
                if gap > 0:
                    state["status"] = "pause"
                    await self._publish_thread(thread_id)
                    await self.bus.log(thread_id, f"Пауза между карточками {gap:.0f} с")
                    await asyncio.sleep(gap)
                    if not self._stop.is_set():
                        state["status"] = "running"
                        await self._publish_thread(thread_id)

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
        for attempt in range(60):
            candidate, _ = self._profiles.acquire(
                settings.view.profile_use_pct, allow_ephemeral=allow_ephemeral
            )
            if candidate is not None:
                # Профиль жёстко привязан к прокси. Если этот прокси уже занят
                # другим потоком, берём следующий профиль: иначе потоки встают
                # в очередь друг за другом, хотя свободные каналы есть.
                assigned = self._assignments.proxy_for(candidate.id, self._proxies)
                if self._proxies.has_capacity(assigned):
                    profile = candidate
                    break
                self._profiles.release(candidate)
                if attempt == 0:
                    await self.bus.log(
                        thread_id,
                        "Ждём профиль, чей прокси не занят другим потоком",
                        "warn",
                    )
                await asyncio.sleep(0.5)
                continue
            if attempt == 0:
                await self.bus.log(
                    thread_id,
                    "Ждём освобождения прогретого профиля (100% переиспользование)",
                    "warn",
                )
            await asyncio.sleep(1)
        if profile is None:
            raise RuntimeError(
                "нет свободного профиля: все привязанные прокси заняты — "
                "добавьте прокси или уменьшите число потоков"
            )

        try:
            for attempt in (1, 2, 3):
                try:
                    return await self._run_browser_once(
                        thread_id, task, settings, steps, state, profile
                    )
                except Exception as exc:  # noqa: BLE001
                    text = str(exc)
                    if attempt >= 3 or not _is_proxy_network_error(text):
                        raise
                    # 407 — прокси не получил логин/пароль. Чаще всего это гонка
                    # на первом запросе: повтор тем же адресом проходит.
                    if _is_proxy_auth_error(text) and attempt == 1:
                        await self.bus.log(
                            thread_id,
                            "Прокси вернул 407 (авторизация): повторяем тем же прокси",
                            "warn",
                        )
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        continue
                    if _is_proxy_auth_error(text) and settings.ltespace.enabled:
                        await self.bus.log(
                            thread_id,
                            "Прокси вернул 407: обновляю доступы LTESpace",
                            "warn",
                        )
                        try:
                            await asyncio.to_thread(sync_proxies, settings)
                            self._proxies.reload()
                        except LtespaceError as refresh_exc:
                            await self.bus.log(
                                thread_id,
                                f"LTESpace: доступы не обновлены — {refresh_exc}",
                                "warn",
                            )
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
            state.update({"profile": None, "proxy": None,
                    "proxy_ip": None, "hwnd": None, "browser_visible": False})
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
                "proxy_ip": None,
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
                # открывает браузер кликом, когда нужно. Прячем сразу после
                # запуска — до любых сетевых проверок, иначе окно «вылазит».
                show = bool(settings.browser.show_on_start)
                win_window.set_visible(session.hwnd, show)
                if not show:
                    # Chrome иногда показывает окно после первого кадра —
                    # прячем повторно, чтобы не «вылазило» перед оператором.
                    await asyncio.sleep(0.3)
                    win_window.set_visible(session.hwnd, False)
                session.visible = show and win_window.is_visible(session.hwnd)
                state["browser_visible"] = session.visible
            await self._publish_thread(thread_id)

            exit_ip = await _proxy_exit_ip(session) if proxy is not None else None
            if exit_ip:
                state["proxy_ip"] = exit_ip
                await self._publish_thread(thread_id)
                await self.bus.log(thread_id, f"Внешний IP прокси: {exit_ip}")
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
            state.update({"profile": None, "proxy": None,
                    "proxy_ip": None, "hwnd": None, "browser_visible": False})
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

