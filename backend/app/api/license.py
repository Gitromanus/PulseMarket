"""REST: лицензия (M7)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..core.config import load_settings, save_settings
from ..core.license import LicenseManager, LicenseError

router = APIRouter(prefix="/api/license", tags=["license"])


class ActivatePayload(BaseModel):
    key: str
    server: str | None = None


@router.get("")
async def status() -> dict:
    settings = load_settings()
    return LicenseManager(server=settings.license.server).status(settings.license.key or None)


@router.post("/activate")
async def activate(payload: ActivatePayload, request: Request) -> dict:
    settings = load_settings()
    if payload.server is not None:
        settings.license.server = payload.server.strip()
    settings.license.key = payload.key.strip()
    manager = LicenseManager(server=settings.license.server)
    try:
        result = manager.activate(payload.key, settings.license.server)
    except LicenseError as exc:
        await request.app.state.bus.log(None, f"Лицензия: {exc}", "error")
        return {"ok": False, "error": str(exc), **manager.status(payload.key, online=False)}

    save_settings(settings)
    await request.app.state.bus.log(
        None,
        f"Лицензия активирована: {result.get('subject') or 'без имени'}"
        f" (до {result.get('expires') or 'бессрочно'})",
        "success",
    )
    return {"ok": True, **result}


@router.post("/check")
async def check(request: Request) -> dict:
    settings = load_settings()
    manager = LicenseManager(server=settings.license.server)
    result = manager.status(settings.license.key or None, online=True)
    if result["enabled"] and not result["valid"]:
        await request.app.state.bus.log(None, f"Лицензия: {result['error']}", "warn")
    return result
