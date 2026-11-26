"""Прокси: разбор файла, выдача потоком и закрепление за профилем."""

from __future__ import annotations

import json
import random
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ..core.config import app_data_dir

_ASSIGN_LOCK = threading.Lock()


@dataclass(frozen=True)
class Proxy:
    raw: str
    server: str
    username: str | None = None
    password: str | None = None

    def playwright(self) -> dict[str, str]:
        data = {"server": self.server}
        if self.username:
            data["username"] = self.username
        if self.password:
            data["password"] = self.password
        return data

    def masked(self) -> str:
        host = self.server.split("://", 1)[-1]
        if self.username:
            return f"{self.server.split('://')[0]}://***:***@{host}"
        return self.server


def parse_proxy(line: str) -> Proxy | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" not in raw:
        raw = f"socks5://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if not parsed.hostname or not parsed.port:
        return None
    scheme = parsed.scheme or "socks5"
    return Proxy(
        raw=raw,
        server=f"{scheme}://{parsed.hostname}:{parsed.port}",
        username=parsed.username,
        password=parsed.password,
    )


@dataclass
class ProxyPool:
    path: str | None = None
    items: list[Proxy] = field(default_factory=list)
    invalid: int = 0
    _busy: set[str] = field(default_factory=set)
    _cursor: int = 0

    def __post_init__(self) -> None:
        if self.path and Path(self.path).exists():
            self.reload()
        # Стартуем с разного прокси при каждом запуске приложения, чтобы
        # соседние прогоны не начинались с одного и того же адреса.
        self._cursor = random.randrange(len(self.items)) if self.items else 0

    def reload(self) -> None:
        self.items.clear()
        self.invalid = 0
        text = Path(self.path).read_text(encoding="utf-8", errors="ignore")  # type: ignore[arg-type]
        for line in text.splitlines():
            proxy = parse_proxy(line)
            if proxy is None:
                if line.strip():
                    self.invalid += 1
                continue
            self.items.append(proxy)

    @property
    def size(self) -> int:
        return len(self.items)

    def find(self, raw: str | None) -> Proxy | None:
        if not raw:
            return None
        for proxy in self.items:
            if proxy.raw == raw:
                return proxy
        return None

    def acquire(self, preferred: str | None = None) -> Proxy | None:
        # С закреплением профиля берём только его прокси; без него — круговой
        # обход пула, чтобы подряд идущие прогоны шли с разных IP.
        total = len(self.items)
        if total == 0:
            return None
        if preferred:
            chosen = self.find(preferred)
            if chosen is None or chosen.raw in self._busy:
                return None
            self._busy.add(chosen.raw)
            return chosen
        for step in range(total):
            index = (self._cursor + step) % total
            chosen = self.items[index]
            if chosen.raw in self._busy:
                continue
            self._cursor = (index + 1) % total
            self._busy.add(chosen.raw)
            return chosen
        return None

    def release(self, proxy: Proxy | None) -> None:
        if proxy is not None:
            self._busy.discard(proxy.raw)

    def state(self) -> dict:
        return {
            "total": self.size,
            "busy": len(self._busy),
            "invalid": self.invalid,
            "file": self.path or "",
        }


def assignments_path() -> Path:
    return app_data_dir() / "profile_proxies.json"


class ProxyAssignments:
    """Постоянное закрепление прокси за профилем.

    Один профиль всегда выходит с одного IP: это важнее «случайной» ротации,
    потому что смена IP у живого профиля выглядит подозрительнее, чем разные
    IP у разных профилей. Новые профили распределяются по пулу равномерно.
    """

    def get(self, profile_id: str) -> str | None:
        with _ASSIGN_LOCK:
            data = self._load()
            value = data["profiles"].get(profile_id)
            return value if isinstance(value, str) else None

    def all(self) -> dict[str, str]:
        """Все закрепления «профиль → прокси» (без логинов в ключах)."""
        with _ASSIGN_LOCK:
            data = self._load()
            return {
                name: raw
                for name, raw in data["profiles"].items()
                if isinstance(name, str) and isinstance(raw, str)
            }

    def proxy_for(self, profile_id: str, pool: ProxyPool) -> Proxy | None:
        if pool.size == 0:
            return None
        with _ASSIGN_LOCK:
            data = self._load()
            profiles: dict[str, str] = data["profiles"]
            assigned = pool.find(profiles.get(profile_id))
            if assigned is not None:
                return assigned
            # Профиль новый или его прокси пропал из файла — выдаём наименее
            # загруженный адрес и запоминаем навсегда.
            pool_raws = {proxy.raw for proxy in pool.items}
            counts = Counter(raw for raw in profiles.values() if raw in pool_raws)
            chosen = min(pool.items, key=lambda proxy: (counts.get(proxy.raw, 0), proxy.raw))
            profiles[profile_id] = chosen.raw
            self._save(data)
            return chosen

    def rebalance(self, profile_ids: list[str], pool: ProxyPool) -> int:
        """Заново раскладывает указанные профили по всему пулу поровну.

        В отличие от proxy_for(), существующие закрепления этих профилей
        сбрасываются — IP у них изменится.
        """
        if pool.size == 0 or not profile_ids:
            return 0
        with _ASSIGN_LOCK:
            data = self._load()
            profiles: dict[str, str] = data["profiles"]
            for profile_id in profile_ids:
                profiles.pop(profile_id, None)
            pool_raws = {proxy.raw for proxy in pool.items}
            counts = Counter(raw for raw in profiles.values() if raw in pool_raws)
            for profile_id in profile_ids:
                chosen = min(pool.items, key=lambda proxy: (counts.get(proxy.raw, 0), proxy.raw))
                profiles[profile_id] = chosen.raw
                counts[chosen.raw] += 1
            self._save(data)
            return len(profile_ids)

    @staticmethod
    def _load() -> dict:
        path = assignments_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                return data
        except Exception:  # noqa: BLE001 — битый файл пересоздаём
            pass
        return {"version": 1, "profiles": {}}

    @staticmethod
    def _save(data: dict) -> None:
        path = assignments_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
