"""Устройственный отпечаток для каждого профиля.

Что меняем:
  * экран и viewport (CSS-пиксели) и DPR;
  * число логических ядер и объём памяти (navigator.hardwareConcurrency /
    navigator.deviceMemory).

Что НЕ меняем сознательно:
  * User-Agent и Sec-CH-UA — они должны совпадать с реальным Chrome, иначе
    получается рассинхрон с TLS/Client Hints;
  * WebGL vendor/renderer, canvas, audio, шрифты — это реальные значения
    машины и GPU; их подмена — самый простой способ спалиться.

Пресеты собраны по публичной статистике: useragents.me (доля Windows Chrome)
и StatCounter (популярные разрешения 1920x1080, 1366x768, 1536x864,
2560x1440). Значения закрепляются за профилем в fingerprints.json, поэтому
один и тот же профиль всегда выглядит одинаково.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..core.config import app_data_dir

STORE_VERSION = 1
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Device:
    preset: str
    viewport_w: int
    viewport_h: int
    screen_w: int
    screen_h: int
    dpr: float
    hardware: int
    memory: int

    def playwright(self) -> dict[str, Any]:
        return {
            "viewport": {"width": self.viewport_w, "height": self.viewport_h},
            "screen": {"width": self.screen_w, "height": self.screen_h},
            "device_scale_factor": self.dpr,
        }

    def describe(self, hardware_spoofed: bool = False) -> str:
        parts = [
            f"{self.viewport_w}x{self.viewport_h} @{self.dpr:g}",
            f"экран {self.screen_w}x{self.screen_h}",
        ]
        if hardware_spoofed:
            parts.append(f"{self.hardware} ядер / {self.memory} ГБ")
        else:
            parts.append("ядра/память реальные")
        parts.append(self.preset)
        return " · ".join(parts)

    def init_script(self) -> str:
        return f"""
(() => {{
  const hardware = {self.hardware};
  const memory = {self.memory};
  const define = (name, value) => {{
    try {{
      Object.defineProperty(Navigator.prototype, name, {{
        get: () => value,
        configurable: true,
        enumerable: true,
      }});
    }} catch (e) {{}}
  }};
  define('hardwareConcurrency', hardware);
  define('deviceMemory', memory);
}})();
"""


# viewport <= screen; screen — CSS-пиксели (физическое разрешение / DPR).
PRESETS: tuple[Device, ...] = (
    Device("fhd-window-8", 1536, 864, 1920, 1080, 1.0, 8, 8),
    Device("fhd-125-8", 1366, 768, 1536, 864, 1.25, 8, 8),
    Device("fhd-150-4", 1280, 680, 1280, 720, 1.5, 4, 4),
    Device("fhd-max-12", 1920, 969, 1920, 1080, 1.0, 12, 8),
    Device("hdplus-8", 1600, 850, 1600, 900, 1.0, 8, 8),
    Device("hd-window-4", 1366, 650, 1366, 768, 1.0, 4, 4),
    Device("qhd-window-16", 1920, 1080, 2560, 1440, 1.0, 16, 8),
    Device("qhd-125-12", 1920, 1080, 2048, 1152, 1.25, 12, 8),
    Device("laptop-125-4", 1366, 768, 1536, 864, 1.25, 4, 4),
    Device("hdplus-window-8", 1440, 810, 1600, 900, 1.0, 8, 4),
    Device("wsxga-8", 1600, 900, 1680, 1050, 1.0, 8, 8),
    Device("uhd-150-16", 1920, 1080, 2560, 1440, 1.5, 16, 8),
)


def spoof_hardware_enabled() -> bool:
    """PF_SPOOF_HARDWARE=0 оставляет реальные ядра/память (диагностика)."""
    return os.getenv("PF_SPOOF_HARDWARE", "1").strip() != "0"


def _store_path() -> Path:
    return app_data_dir() / "fingerprints.json"


def _load() -> dict[str, Any]:
    path = _store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
            return data
    except Exception:  # noqa: BLE001 — битый файл пересоздаём
        pass
    return {"version": STORE_VERSION, "profiles": {}}


def _save(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def device_for(profile_id: str) -> Device:
    """Стабильный отпечаток профиля; при первом обращении назначаем пресет."""
    with _LOCK:
        data = _load()
        profiles: dict[str, Any] = data.setdefault("profiles", {})
        entry = profiles.get(profile_id)
        if isinstance(entry, dict) and entry.get("preset"):
            try:
                return Device(
                    preset=str(entry["preset"]),
                    viewport_w=int(entry["viewport_w"]),
                    viewport_h=int(entry["viewport_h"]),
                    screen_w=int(entry["screen_w"]),
                    screen_h=int(entry["screen_h"]),
                    dpr=float(entry["dpr"]),
                    hardware=int(entry["hardware"]),
                    memory=int(entry["memory"]),
                )
            except (KeyError, TypeError, ValueError):
                pass

        used = {
            str(item.get("preset"))
            for item in profiles.values()
            if isinstance(item, dict) and item.get("preset")
        }
        free = [preset for preset in PRESETS if preset.preset not in used]
        candidates = free or list(PRESETS)
        seed = int(hashlib.sha256(profile_id.encode("utf-8")).hexdigest()[:8], 16)
        chosen = candidates[seed % len(candidates)]
        profiles[profile_id] = asdict(chosen)
        _save(data)
        return chosen


def describe_store() -> dict[str, Any]:
    """Для диагностики/тестов: текущие назначения."""
    with _LOCK:
        return _load()
