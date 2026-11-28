"""Настройки приложения: модель, загрузка/сохранение в %APPDATA%/pf-automation."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def app_data_dir() -> Path:
    base = os.getenv("PF_APP_DATA") or os.getenv("APPDATA") or str(Path.home())
    path = Path(base) / "pf-automation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def runtime_path() -> Path:
    return app_data_dir() / "runtime.json"


def db_path() -> Path:
    return app_data_dir() / "app.db"


# Windows-идентификатор пояса → IANA. Chromium принимает только IANA.
WINDOWS_TZ = {
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Russian Standard Time": "Europe/Moscow",
    "Samara Standard Time": "Europe/Samara",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Omsk Standard Time": "Asia/Omsk",
    "Novosibirsk Standard Time": "Asia/Novosibirsk",
    "Krasnoyarsk Standard Time": "Asia/Krasnoyarsk",
    "Irkutsk Standard Time": "Asia/Irkutsk",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Magadan Standard Time": "Asia/Magadan",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Kamchatka Standard Time": "Asia/Kamchatka",
    "UTC": "UTC",
}


def system_timezone() -> str:
    """IANA-имя системного пояса (для логов и проверки согласованности).

    Если `timezone_id` не передавать, Chromium берёт пояс из ОС — это и нужно,
    чтобы браузер не «врал» относительно системы и геолокации IP.
    """
    env = os.getenv("TZ", "").strip()
    if env:
        return env
    if os.name == "nt":
        try:
            import subprocess

            key = subprocess.run(
                ["tzutil", "/g"], capture_output=True, text=True, timeout=3
            ).stdout.strip()
            if key in WINDOWS_TZ:
                return WINDOWS_TZ[key]
        except Exception:  # noqa: BLE001
            pass
    offset = datetime.now().astimezone().utcoffset()
    if offset is not None:
        hours = int(offset.total_seconds() // 3600)
        # В Etc/GMT знак инвертирован: UTC+5 → Etc/GMT-5.
        return f"Etc/GMT{-hours:+d}" if hours else "Etc/GMT"
    return "UTC"


class QuickActions(BaseModel):
    cart_from_search: bool = False
    goto_cart: bool = False
    view_photos: bool = True
    favorite: bool = False


class GeneralSettings(BaseModel):
    fingerprint_key: str = ""
    proxies_file: str = ""
    dir_active: str = ""
    dir_done: str = ""
    dir_noauth: str = ""
    threads: int = Field(default=4, ge=1, le=64)
    # Сколько потоков одновременно может работать через один прокси.
    proxy_max_threads: int = Field(default=1, ge=1, le=64)
    # Задержка между запуском потоков: снижает одновременный залп браузеров.
    thread_start_interval_sec: int = Field(default=0, ge=0, le=600)
    # Пауза между карточками внутри потока (случайная в этих границах).
    task_gap_min_sec: int = Field(default=20, ge=0, le=3600)
    task_gap_max_sec: int = Field(default=70, ge=0, le=3600)
    repeat_limit: int = Field(default=0, ge=0)
    # Пауза перед повторным заходом в ту же строку. По умолчанию выключена:
    # каждый прогон идёт с нового профиля и прокси, поэтому смысла ждать нет.
    repeat_cooldown_min: int = Field(default=0, ge=0, le=1440)
    error_limit: int = Field(default=10, ge=0)
    sync_enabled: bool = True
    sync_interval_sec: int = Field(default=30, ge=10, le=600)
    schedule_enabled: bool = False
    schedule_interval_min: int = Field(default=60, ge=5, le=1440)
    quick_actions: QuickActions = Field(default_factory=QuickActions)

    @model_validator(mode="after")
    def _check_gap(self) -> "GeneralSettings":
        if self.task_gap_max_sec < self.task_gap_min_sec:
            raise ValueError("task_gap_max_sec: максимум должен быть >= минимума")
        return self


class ViewSettings(BaseModel):
    profile_use_pct: int = Field(default=60, ge=0, le=100)
    like_pct: int = Field(default=30, ge=0, le=100)
    cart_pct: int = Field(default=15, ge=0, le=100)
    other_sellers_pct: int = Field(default=10, ge=0, le=100)
    see_also_pct: int = Field(default=10, ge=0, le=100)
    compare_pct: int = Field(default=5, ge=0, le=100)
    review_like_pct: int = Field(default=10, ge=0, le=100)
    max_search_pages: int = Field(default=10, ge=1, le=20)
    card_time_sec: tuple[int, int] = (180, 320)
    reviews_time_sec: tuple[int, int] = (40, 90)

    @model_validator(mode="after")
    def _check_ranges(self) -> "ViewSettings":
        for name in ("card_time_sec", "reviews_time_sec"):
            low, high = getattr(self, name)
            if low < 1 or high < low:
                raise ValueError(f"{name}: требуется 0 < min <= max")
        return self


class BrowserSettings(BaseModel):
    provider: Literal["vanilla", "camoufox", "cdp"] = "vanilla"
    cdp_endpoint: str = "http://127.0.0.1:3001"
    channel: Literal["auto", "chrome", "edge", "chromium"] = "auto"
    headless: bool = False
    # Показывать окно браузера сразу после запуска. По умолчанию скрыто:
    # оператор смотрит живое превью и открывает окно по клику.
    show_on_start: bool = False
    locale: str = "ru-RU"
    timezone: str = ""            # пусто — системный пояс (как у ОС и IP)
    enabled: bool = False           # потоки открывают реальный браузер
    check_url: str = "https://example.com/"


class LicenseSettings(BaseModel):
    key: str = ""
    server: str = ""


class LtespaceSettings(BaseModel):
    """Мобильные прокси LTESpace: ключ, файл пула и смена IP при блоке."""

    api_key: str = ""
    enabled: bool = False
    # Отдельный файл: ручные прокси из general.proxies_file не затираются.
    proxies_file: str = ""
    sync_proxies: bool = True
    rotate_on_block: bool = True
    # Менять IP всех прокси с ручной ротацией перед каждым прогоном.
    rotate_before_run: bool = False
    # Капча-пазл: сразу остановить прогон, сменить IP и запустить заново.
    restart_on_captcha: bool = True
    captcha_restart_delay_sec: int = Field(default=20, ge=0, le=600)
    captcha_restart_limit: int = Field(default=3, ge=0, le=20)
    protocol: Literal["http", "socks", "auto"] = "http"


class AppSettings(BaseModel):
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    view: ViewSettings = Field(default_factory=ViewSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    license: LicenseSettings = Field(default_factory=LicenseSettings)
    ltespace: LtespaceSettings = Field(default_factory=LtespaceSettings)


def load_settings() -> AppSettings:
    path = settings_path()
    if not path.exists():
        data = AppSettings().model_dump()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return AppSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        browser = raw.get("browser")
        if isinstance(browser, dict) and browser.get("timezone") == "Europe/Moscow":
            # Старое значение по умолчанию: пояс должен совпадать с системой/IP.
            browser["timezone"] = ""
        return AppSettings.model_validate(raw)
    except (json.JSONDecodeError, ValueError):
        # Битый файл не должен блокировать запуск: уводим в .bak и стартуем с дефолтов.
        path.replace(path.with_suffix(".json.bak"))
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    path = settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def write_runtime(port: int) -> dict:
    """Electron читает этот файл, чтобы узнать порт и токен локального API."""
    info = {"port": port, "token": secrets.token_urlsafe(24), "pid": os.getpid()}
    runtime_path().write_text(json.dumps(info), encoding="utf-8")
    return info
