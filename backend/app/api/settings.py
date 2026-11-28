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
    ltespace = payload.ltespace
    if ltespace.enabled and ltespace.api_key and ltespace.sync_proxies:
        # Включили LTESpace — сразу делаем его пулом, иначе проверка прокси
        # и закрепление профилей работали бы со старым файлом.
        from ..integrations.ltespace import LtespaceError, sync_proxies

        try:
            result = await asyncio.to_thread(sync_proxies, payload)
            await request.app.state.bus.log(
                None,
                f"LTESpace: пул обновлён — {result['written']} прокси, "
                f"перезакреплено профилей {result['pinned']} из {result['profiles']}",
            )
        except LtespaceError as exc:
            await request.app.state.bus.log(
                None, f"LTESpace: прокси не обновлены — {exc}", "warn"
            )
    sync = getattr(request.app.state, "sync", None)
    if sync is not None:
        # Сразу обновляем панель: счётчики прогресса должны быть свежими.
        state = sync.status()
        await request.app.state.bus.publish("sync_state", state)
        asyncio.create_task(sync.cycle())
    return {"ok": True}


@router.post("/validate")
async def validate_settings(payload: AppSettings) -> dict:
    warnings: list[str] = []
    general = payload.general

    for label, value in (
        ("Список прокси", general.proxies_file),
        ("Папка активных профилей", general.dir_active),
        ("Папка отработанных профилей", general.dir_done),
        ("Папка безакков", general.dir_noauth),
    ):
        if value and not Path(value).exists():
            warnings.append(f"{label}: путь не найден — {value}")

    ltespace = payload.ltespace
    if ltespace.enabled and not ltespace.api_key:
        warnings.append("LTESpace включён, но API-ключ не задан")
    if ltespace.api_key:
        from ..integrations.ltespace import LtespaceClient, LtespaceError

        try:
            balance = await asyncio.to_thread(LtespaceClient(ltespace.api_key).balance)
            if not float(balance.get("balance") or 0):
                warnings.append(
                    "LTESpace: баланс 0 — продлить прокси автоматически не получится"
                )
        except LtespaceError as exc:
            warnings.append(f"LTESpace: {exc}")

    return {"ok": not warnings, "warnings": warnings}
