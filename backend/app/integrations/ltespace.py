"""Клиент LTESpace API: мобильные прокси, баланс и смена IP.

Спецификация: https://api.ltespace.com/openapi.json
Ключ выпускается в кабинете «Настройки → API» и передаётся заголовком
``Authorization: Bearer <api_key>``. Ключ показывается один раз, новый ключ
отзывает предыдущий.

Модуль синхронный (urllib, как и ``core/license.py``). Из async-кода его
нужно вызывать через ``asyncio.to_thread``: сеть не должна блокировать
событийный цикл FastAPI.
"""

from __future__ import annotations

import base64
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..core.config import AppSettings, app_data_dir, save_settings
from ..browser.proxies import Proxy, parse_proxy

DEFAULT_BASE = "https://api.ltespace.com"
DEFAULT_FILE = "proxies_ltespace.txt"

# В файл прокси и в Playwright отдаём схему подключения, а не «режим» порта.
SCHEME = {"http": "http", "socks": "socks5", "auto": "http"}


class LtespaceError(Exception):
    """Ошибка LTESpace: текст пригоден для показа оператору."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def default_proxies_path() -> Path:
    return app_data_dir() / DEFAULT_FILE


class LtespaceClient:
    """Тонкая обёртка над публичным API LTESpace."""

    def __init__(self, api_key: str, base: str = DEFAULT_BASE, timeout: float = 15.0) -> None:
        self.api_key = (api_key or "").strip()
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ---------- транспорт ----------

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise LtespaceError("Не задан API-ключ LTESpace")
        url = f"{self.base}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }
        if body is not None:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except OSError:
                pass
            raise LtespaceError(
                f"LTESpace отклонил запрос (HTTP {exc.code}) {detail}".strip()
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LtespaceError(f"LTESpace недоступен: {exc}") from exc

        try:
            answer = json.loads(raw or "{}")
        except ValueError as exc:
            raise LtespaceError("LTESpace вернул некорректный ответ") from exc
        if not isinstance(answer, dict):
            raise LtespaceError("LTESpace вернул некорректный ответ")
        # Бизнес-ошибки приходят с HTTP 200 и status="error": проверяем тело.
        if answer.get("status") != "ok":
            error = answer.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            message = answer.get("message") or "LTESpace отклонил запрос"
            raise LtespaceError(str(message), code=str(code) if code else None)
        return answer

    # ---------- методы API ----------

    def balance(self) -> dict[str, Any]:
        return self._call("GET", "/user/balance")

    def profile(self) -> dict[str, Any]:
        return self._call("GET", "/user/profile")

    def proxies(self, order_id: int | None = None) -> list[dict[str, Any]]:
        path = "/proxies"
        if order_id is not None:
            path += f"?order_id={int(order_id)}"
        answer = self._call("GET", path)
        items = answer.get("proxies")
        return items if isinstance(items, list) else []

    def set_rotation(self, port: int, minutes: int) -> dict[str, Any]:
        return self._call(
            "PATCH",
            f"/proxies/{int(port)}/rotation",
            {"rotation": max(0, min(60, int(minutes)))},
        )

    def rotate_link(self, url: str, timeout: float = 30.0) -> dict[str, Any]:
        """Ручная ротация приватного тарифа: обычный GET без авторизации.

        Ответ сервиса содержит ``old_ip``/``new_ip`` — возвращаем его, чтобы
        показать оператору, что IP действительно сменился.
        """
        if not url:
            raise LtespaceError("У прокси нет ссылки ручной ротации")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")[:500]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LtespaceError(f"Ссылка ротации недоступна: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError:
            return {"raw": raw}
        if isinstance(data, dict) and data.get("status") == "error":
            raise LtespaceError(str(data.get("error") or "ротация отклонена"))
        return data if isinstance(data, dict) else {"raw": raw}


# ---------- синхронизация списка прокси ----------


def proxy_lines(items: list[dict[str, Any]], protocol: str = "http") -> tuple[list[str], int]:
    """Собирает строки ``scheme://login:pass@host:port`` из ответа ``GET /proxies``.

    Возвращает (строки, сколько прокси пропущено). Просроченные и
    замороженные каналы в пул не попадают: с ними только лишние ошибки.
    """
    scheme = SCHEME.get(protocol, "http")
    lines: list[str] = []
    skipped = 0
    for item in items:
        login = str(item.get("login") or "").strip()
        password = str(item.get("password") or "").strip()
        host = str(item.get("host") or "").strip()
        port = item.get("port")
        if not (login and password and host and port):
            skipped += 1
            continue
        if not item.get("active") or int(item.get("frozen") or 0):
            skipped += 1
            continue
        expire_ts = item.get("expire_ts")
        if expire_ts and int(expire_ts) <= int(time.time()):
            skipped += 1
            continue
        lines.append(f"{scheme}://{login}:{password}@{host}:{int(port)}")
    return lines, skipped


def mask_item(item: dict[str, Any]) -> dict[str, Any]:
    """Прокси без пароля — для логов и ответов локального API."""
    return {
        "id": item.get("id"),
        "port": item.get("port"),
        "host": item.get("host"),
        "ip": item.get("ip"),
        "masked": f"{item.get('host')}:{item.get('port')}",
        "tariff": item.get("tariff"),
        "operator": item.get("operator"),
        "city": item.get("city"),
        "country": item.get("country"),
        "rotation": item.get("rotation"),
        "expire": item.get("expire"),
        "active": bool(item.get("active")),
        "manual_rotation_available": bool(item.get("manual_rotation_available")),
    }


def sync_proxies(
    settings: AppSettings,
    client: LtespaceClient | None = None,
) -> dict[str, Any]:
    """Забирает прокси из LTESpace и перезаписывает файл пула.

    Файл прокси отдельный (по умолчанию ``proxies_ltespace.txt``), но при
    включённой интеграции он становится основным: ``general.proxies_file``
    указывает на него, поэтому потоки, список прокси и проверка на Ozon
    работают именно с выбранными прокси LTE. Профили, привязанные к старым
    адресам, сразу перезакрепляются за новым пулом.
    """
    ls = settings.ltespace
    if not ls.api_key:
        raise LtespaceError("Не задан API-ключ LTESpace")
    client = client or LtespaceClient(ls.api_key)
    items = client.proxies()
    lines, skipped = proxy_lines(items, ls.protocol)

    target = Path(ls.proxies_file) if ls.proxies_file else default_proxies_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(target)

    changed = False
    if settings.general.proxies_file != str(target):
        # Выбранные прокси LTESpace становятся основным пулом: и потоки,
        # и «Список прокси», и проверка на Ozon смотрят в general.proxies_file.
        settings.general.proxies_file = str(target)
        save_settings(settings)
        changed = True

    # Закрепления старых прокси больше не существуют в новом пуле — сразу
    # раскладываем такие профили по актуальным каналам, не дожидаясь кнопки.
    pinned = 0
    active = 0
    try:
        from ..browser.profiles import ProfileStore
        from ..browser.proxies import ProxyAssignments, ProxyPool

        pool = ProxyPool(str(target))
        assignments = ProxyAssignments()
        profiles = ProfileStore(settings).list("active")
        active = len(profiles)
        for profile in profiles:
            if pool.find(assignments.get(profile.id)) is None:
                assignments.proxy_for(profile.id, pool)
                pinned += 1
    except Exception:  # noqa: BLE001 — закрепление не должно ломать синхронизацию
        pass

    return {
        "ok": True,
        "file": str(target),
        "written": len(lines),
        "skipped": skipped,
        "settings_changed": changed,
        "pinned": pinned,
        "profiles": active,
        "proxies": [mask_item(item) for item in items],
    }


def _pool_ports(settings: AppSettings) -> set[int]:
    """Порты прокси, которые реально лежат в пуле (general.proxies_file)."""
    raw = settings.general.proxies_file or settings.ltespace.proxies_file
    path = Path(raw) if raw else default_proxies_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    ports: set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tail = line.rsplit(":", 1)[-1].split("/")[0].strip()
        if tail.isdigit():
            ports.add(int(tail))
    return ports


def auto_rotation_snapshot(settings: AppSettings) -> dict[int, dict[str, Any]]:
    """Прокси без ручной ротации: текущий IP и интервал автосмены (минуты).

    Нужно, чтобы после капчи не просто остановить прогон, а дождаться,
    когда shared-прокси сам сменит IP по таймеру, и продолжить работу.
    """
    ls = settings.ltespace
    if not ls.api_key:
        return {}
    ports = _pool_ports(settings)
    result: dict[int, dict[str, Any]] = {}
    for item in LtespaceClient(ls.api_key).proxies():
        if item.get("manual_rotation_url"):
            continue
        port = int(item.get("port") or 0)
        if port <= 0 or (ports and port not in ports):
            continue
        rotation = int(item.get("rotation") or 0)
        ip = str(item.get("ip") or "").strip()
        if rotation <= 0 or not ip:
            continue
        result[port] = {"ip": ip, "rotation": rotation}
    return result


def pool_proxies(settings: AppSettings) -> list[Proxy]:
    """Прокси из текущего пула (general.proxies_file)."""
    raw = settings.general.proxies_file or settings.ltespace.proxies_file
    path = Path(raw) if raw else default_proxies_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    proxies: list[Proxy] = []
    for line in text.splitlines():
        proxy = parse_proxy(line)
        if proxy is not None:
            proxies.append(proxy)
    return proxies


def exit_ip_via_proxy(proxy: Proxy, timeout: float = 15.0) -> str | None:
    """Реальный внешний IP через прокси (CONNECT к api.ipify.org).

    Поле ``ip`` в ответе LTESpace статичное и не отражает смену IP по
    таймеру, поэтому ждать автосмену можно только по фактическому IP.
    """
    parsed = urllib.parse.urlparse(proxy.server)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.port:
        return None
    headers: dict[str, str] = {}
    if proxy.username:
        token = base64.b64encode(
            f"{proxy.username}:{proxy.password or ''}".encode("utf-8")
        ).decode("ascii")
        headers["Proxy-Authorization"] = f"Basic {token}"
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        conn.set_tunnel("api.ipify.org", 443, headers=headers)
        conn.request(
            "GET",
            "/",
            headers={"Host": "api.ipify.org", "User-Agent": "PulseMarket/1.0"},
        )
        response = conn.getresponse()
        if response.status != 200:
            return None
        return response.read().decode("utf-8", errors="replace").strip() or None
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def proxy_exit_ips(settings: AppSettings, timeout: float = 30.0) -> dict[int, str]:
    """Внешние IP всех прокси пула: {порт: IP}."""
    proxies = pool_proxies(settings)
    if not proxies:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    def one(proxy: Proxy) -> tuple[int | None, str | None]:
        return port_of(proxy), exit_ip_via_proxy(proxy, timeout)

    result: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(proxies))) as pool:
        for port, ip in pool.map(one, proxies):
            if port and ip:
                result[port] = ip
    return result


# ---------- смена IP ----------


def port_of(proxy: Proxy | None) -> int | None:
    if proxy is None:
        return None
    try:
        return urllib.parse.urlparse(proxy.server).port
    except ValueError:
        return None


def rotate_all(settings: AppSettings, ports: list[int] | None = None) -> dict[str, Any]:
    """Меняет IP у всех прокси с ручной ротацией (или у указанных портов).

    Ссылки ротации дергаются параллельно: каждая занимает 5–10 секунд, иначе
    прогон на 10 каналах ждал бы больше минуты.
    """
    ls = settings.ltespace
    if not ls.api_key:
        raise LtespaceError("Не задан API-ключ LTESpace")
    client = LtespaceClient(ls.api_key)
    items = client.proxies()
    wanted = set(ports) if ports else None
    target = []
    skipped: list[int] = []
    for item in items:
        port = int(item.get("port") or 0)
        if wanted is not None and port not in wanted:
            continue
        if not item.get("manual_rotation_url"):
            skipped.append(port)
            continue
        target.append(item)

    errors: dict[int, str] = {}
    rotated: list[int] = []
    new_ips: dict[int, str] = {}
    if target:
        from concurrent.futures import ThreadPoolExecutor

        def rotate_one(item: dict[str, Any]) -> None:
            port = int(item.get("port") or 0)
            try:
                answer = client.rotate_link(str(item["manual_rotation_url"]))
                rotated.append(port)
                if answer.get("new_ip"):
                    new_ips[port] = str(answer["new_ip"])
            except LtespaceError as exc:
                errors[port] = str(exc)

        with ThreadPoolExecutor(max_workers=min(8, len(target))) as pool:
            list(pool.map(rotate_one, target))

    return {
        "rotated": rotated,
        "skipped": skipped,
        "errors": errors,
        "new_ips": new_ips,
    }


def rotate_for_proxy(settings: AppSettings, proxy: Proxy | None) -> dict[str, Any]:
    """Меняет IP прокси, закреплённого за потоком, если тариф это позволяет.

    На приватном тарифе используется ссылка ручной ротации. На shared
    ручной ротации нет — там IP меняется по таймеру, поэтому возвращаем
    понятный отказ вместо «тихого» бездействия.
    """
    port = port_of(proxy)
    if port is None:
        return {"ok": False, "error": "У потока нет прокси LTESpace"}
    client = LtespaceClient(settings.ltespace.api_key)
    items = client.proxies()
    item = next((row for row in items if int(row.get("port") or 0) == port), None)
    if item is None:
        raise LtespaceError(f"Порт {port} не найден в аккаунте LTESpace")
    link = item.get("manual_rotation_url")
    if link:
        answer = client.rotate_link(str(link))
        return {
            "ok": True,
            "mode": "manual",
            "port": port,
            "tariff": item.get("tariff"),
            "old_ip": answer.get("old_ip"),
            "new_ip": answer.get("new_ip"),
        }
    return {
        "ok": False,
        "mode": "timer",
        "port": port,
        "tariff": item.get("tariff"),
        "error": (
            f"Тариф {item.get('tariff')} меняет IP только по таймеру "
            f"({item.get('rotation')} мин) — ручной ротации нет"
        ),
    }
