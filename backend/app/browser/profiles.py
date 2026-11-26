"""Профили браузера: активные (аккаунты), отработанные, безаки."""

from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.config import AppSettings, app_data_dir
from . import fingerprints


@dataclass(frozen=True)
class Profile:
    folder: Path
    kind: str  # active | done | noauth
    id: str

    @property
    def label(self) -> str:
        return self.folder.name

    @property
    def is_new(self) -> bool:
        """Пустой профиль: Chrome ещё ни разу не запускался в этой папке."""
        return not (self.folder / "Default").exists()


class ProfileStore:
    def __init__(self, settings: AppSettings) -> None:
        general = settings.general
        self.dirs: dict[str, Path | None] = {
            "active": Path(general.dir_active) if general.dir_active else None,
            "done": Path(general.dir_done) if general.dir_done else None,
            "noauth": Path(general.dir_noauth) if general.dir_noauth else None,
        }
        self._noauth_default = app_data_dir() / "profiles" / "noauth"
        self._busy: set[str] = set()
        # Случайная стартовая точка на каждый запуск приложения: между
        # прогонами первыми берутся разные профили.
        self._cursor = random.randrange(64)

    def refresh(self, settings: AppSettings) -> None:
        """Обновляет папки из настроек, сохраняя позицию кругового обхода."""
        general = settings.general
        self.dirs = {
            "active": Path(general.dir_active) if general.dir_active else None,
            "done": Path(general.dir_done) if general.dir_done else None,
            "noauth": Path(general.dir_noauth) if general.dir_noauth else None,
        }

    def _dir(self, kind: str) -> Path:
        if kind == "noauth":
            return self.dirs["noauth"] or self._noauth_default
        configured = self.dirs.get(kind)
        if configured is None:
            raise FileNotFoundError(f"Не задана папка профилей: {kind}")
        return configured

    def list(self, kind: str) -> list[Profile]:
        try:
            folder = self._dir(kind)
        except FileNotFoundError:
            return []
        if not folder.exists():
            return []
        return [
            Profile(folder=child, kind=kind, id=child.name)
            for child in sorted(folder.iterdir())
            if child.is_dir()
        ]

    def create_ephemeral(self) -> Profile:
        folder = self._dir("noauth")
        folder.mkdir(parents=True, exist_ok=True)
        name = f"auto-{datetime.now().strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        target = folder / name
        target.mkdir(parents=True, exist_ok=True)
        return Profile(folder=target, kind="noauth", id=name)

    def create_many(self, count: int) -> list[Profile]:
        """Создаёт count пустых профилей в active и выдаёт каждому отпечаток."""
        folder = self._dir("active")
        folder.mkdir(parents=True, exist_ok=True)
        existing = {child.name for child in folder.iterdir() if child.is_dir()}
        prefix = "ozon-fresh" if any(n.startswith("ozon-fresh-") for n in existing) else "profile"
        pattern = re.compile(rf"{re.escape(prefix)}-(\d+)$")
        next_index = max(
            (int(match.group(1)) for name in existing if (match := pattern.match(name))),
            default=0,
        ) + 1
        created: list[Profile] = []
        for _ in range(count):
            while f"{prefix}-{next_index:02d}" in existing:
                next_index += 1
            name = f"{prefix}-{next_index:02d}"
            next_index += 1
            target = folder / name
            target.mkdir()
            fingerprints.device_for(name)
            existing.add(name)
            created.append(Profile(folder=target, kind="active", id=name))
        return created

    def acquire(
        self, use_profile_pct: int, allow_ephemeral: bool = True
    ) -> tuple[Profile | None, bool]:
        """Возвращает (профиль, был ли использован готовый аккаунт)."""
        active = self.list("active")
        if random.randint(1, 100) <= use_profile_pct:
            total = len(active)
            # Круговой обход: каждый следующий прогон берёт следующий свободный
            # профиль, поэтому подряд идущие прогоны не сидят на одной папке.
            for step in range(total):
                index = (self._cursor + step) % total
                chosen = active[index]
                if str(chosen.folder) in self._busy:
                    continue
                self._cursor = (index + 1) % total
                self._busy.add(str(chosen.folder))
                return chosen, True
            # 100% переиспользование: ждём освобождения, а не подсовываем холодный.
            if not allow_ephemeral and active:
                return None, False
        if not allow_ephemeral and active:
            return None, False
        return self.create_ephemeral(), False

    def release(self, profile: Profile | None) -> None:
        if profile is not None:
            self._busy.discard(str(profile.folder))

    def state(self) -> dict:
        active = self.list("active")
        return {
            "active": len(active),
            "new": sum(1 for profile in active if profile.is_new),
            "done": len(self.list("done")),
            "noauth": len(self.list("noauth")),
            "busy": len(self._busy),
            "dirs": {kind: str(path) if path else "" for kind, path in self.dirs.items()},
        }
