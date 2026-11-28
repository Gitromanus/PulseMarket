"""REST: интеграция с LTESpace (мобильные прокси, баланс, смена IP)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..core.config import load_settings
from ..integrations.ltespace import (
    LtespaceClient,
    LtespaceError,
    default_proxies_path,
    mask_item,
    rotate_for_proxy,
    sync_proxies,
)

router = APIRouter(prefix="/api/ltespace", tags=["ltespace"])


class RotatePayload(BaseModel):
    port: int = Field(ge=1, le=65535)


def _state_error(message: str) -> dict:
    return {
        "ok": False,
        "enabled": False,
        "balance": None,
        "currency": None,
        "proxies": [],
        "file": "",
        "error": message,
    }


@router.get("/status")
async def status() -> dict:
    """Баланс и прокси аккаунта. Ключ и пароли наружу не отдаются."""
    settings = load_settings()
    ls = settings.ltespace
    if not ls.api_key:
        return _state_error("API-ключ LTESpace не задан")
    client = LtespaceClient(ls.api_key)
    try:
        balance = await asyncio.to_thread(client.balance)
        items = await asyncio.to_thread(client.proxies)
    except LtespaceError as exc:
        result = _state_error(str(exc))
        result["enabled"] = ls.enabled
        return result

    return {
        "ok": True,
        "enabled": ls.enabled,
        "balance": balance.get("balance"),
        "currency": balance.get("currency"),
        "proxies": [mask_item(item) for item in items],
        "file": ls.proxies_file or str(default_proxies_path()),
        "error": None,
    }


@router.post("/sync")
async def sync(request: Request) -> dict:
    """Забрать прокси из LTESpace и перезаписать файл пула."""
    settings = load_settings()
    try:
        result = await asyncio.to_thread(sync_proxies, settings)
    except LtespaceError as exc:
        await request.app.state.bus.log(None, f"LTESpace: {exc}", "error")
        return {"ok": False, "error": str(exc)}

    await request.app.state.bus.log(
        None,
        f"LTESpace: прокси обновлены — {result['written']} в пуле, "
        f"пропущено {result['skipped']} · {result['file']}",
    )
    sync_state = getattr(request.app.state, "sync", None)
    if sync_state is not None:
        await request.app.state.bus.publish("sync_state", sync_state.status())
    return result


@router.post("/rotate")
async def rotate(payload: RotatePayload, request: Request) -> dict:
    """Сменить IP прокси вручную (приватный тариф)."""
    settings = load_settings()
    result: dict = {}
    try:
        result = await asyncio.to_thread(_rotate_port, settings, payload.port)
    except LtespaceError as exc:
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        await request.app.state.bus.log(
            None,
            f"LTESpace: IP порта {payload.port} сменён вручную",
        )
    else:
        await request.app.state.bus.log(
            None,
            f"LTESpace: смена IP порта {payload.port} не выполнена — {result.get('error')}",
            "warn",
        )
    return result


def _rotate_port(settings, port: int) -> dict:
    from ..browser.proxies import Proxy

    fake = Proxy(raw=f"http://***:***@host:{int(port)}", server=f"http://host:{int(port)}")
    return rotate_for_proxy(settings, fake)
