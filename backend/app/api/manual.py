"""REST: ручной режим (M6)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..browser.profiles import ProfileStore
from ..browser.proxies import parse_proxy
from ..core.config import load_settings
from ..core.license import require_license

router = APIRouter(prefix="/api/manual", tags=["manual"])


class StartManual(BaseModel):
    profile_path: str | None = None
    proxy: str | None = None
    url: str | None = None


class Visible(BaseModel):
    visible: bool


class Navigate(BaseModel):
    url: str


@router.get("")
async def list_sessions(request: Request) -> dict:
    return {"items": request.app.state.manual.list()}


@router.post("/start")
async def start(request: Request, payload: StartManual | None = None) -> dict:
    payload = payload or StartManual()
    license_error = require_license()
    if license_error:
        await request.app.state.bus.log(None, license_error, "error")
        return {"ok": False, "error": license_error}
    settings = load_settings()
    result = await request.app.state.manual.start(
        settings,
        profile_path=payload.profile_path,
        proxy_raw=payload.proxy,
        url=payload.url,
        busy_paths=request.app.state.sup.busy_profile_paths(),
    )
    if not result.get("ok"):
        await request.app.state.bus.log(None, f"Ручной режим: {result.get('error')}", "error")
    return result


@router.post("/{sid}/browser")
async def visibility(sid: str, payload: Visible, request: Request) -> dict:
    return await request.app.state.manual.set_visible(sid, payload.visible)


@router.post("/{sid}/navigate")
async def navigate(sid: str, payload: Navigate, request: Request) -> dict:
    return await request.app.state.manual.navigate(sid, payload.url)


@router.post("/{sid}/close")
async def close(sid: str, request: Request) -> dict:
    return await request.app.state.manual.close(sid)


@router.get("/preview")
async def preview(profile_path: str | None = None, proxy: str | None = None) -> dict:
    """Проверка введённых значений без запуска браузера."""
    errors: list[str] = []
    if profile_path:
        from pathlib import Path

        if not Path(profile_path).exists():
            errors.append("Папка профиля не найдена")
    parsed = parse_proxy(proxy) if proxy else None
    if proxy and parsed is None:
        errors.append("Прокси не разобран: ожидается socks5://user:pass@host:port")
    return {"ok": not errors, "errors": errors, "proxy": parsed.masked() if parsed else "direct"}


@router.get("/profiles")
async def profiles() -> dict:
    store = ProfileStore(load_settings())
    return {
        kind: [{"name": profile.label, "path": str(profile.folder)} for profile in store.list(kind)]
        for kind in ("active", "done", "noauth")
    }
