"""REST: настройки."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request

from ..core.config import AppSettings, load_settings, save_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings() -> dict:
    return load_settings().model_dump()


@router.put("")
async def put_settings(payload: AppSettings, request: Request) -> dict:
    save_settings(payload)
    await request.app.state.bus.log(None, "Настройки сохранены")
    sync = getattr(request.app.state, "sync", None)
    if sync is not None:
        # Сразу обновляем панель и запускаем цикл: новый Excel/путь должен
        # подхватиться без ожидания интервала автосинхронизации.
        state = sync.status()
        await request.app.state.bus.publish("sync_state", state)
        asyncio.create_task(sync.cycle())
    return {"ok": True}


@router.post("/validate")
async def validate_settings(payload: AppSettings) -> dict:
    warnings: list[str] = []
    general = payload.general

    for label, value in (
        ("Google credentials", general.google_credentials),
        ("Список прокси", general.proxies_file),
        ("Таблица Excel", general.sheet_xlsx),
        ("Папка активных профилей", general.dir_active),
        ("Папка отработанных профилей", general.dir_done),
        ("Папка безакков", general.dir_noauth),
    ):
        if value and not Path(value).exists():
            warnings.append(f"{label}: путь не найден — {value}")

    if not general.spreadsheet_id and not general.sheet_xlsx:
        warnings.append(
            "Не задана таблица: ни ID Google, ни файл Excel — работает тестовая таблица"
        )
    if general.spreadsheet_id and not general.google_credentials:
        warnings.append("Указан ID Google-таблицы, но не выбран файл сервисного аккаунта")

    return {"ok": not warnings, "warnings": warnings}
