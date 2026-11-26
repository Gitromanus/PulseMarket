"""REST: Google Sheets / тестовая таблица."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/sheets", tags=["sheets"])


@router.get("/status")
async def status(request: Request) -> dict:
    return request.app.state.sync.status()


@router.post("/test")
async def test_connection(request: Request) -> dict:
    result = await request.app.state.sync.probe()
    if result.get("ok"):
        await request.app.state.bus.log(
            None, f"Таблица доступна ({result.get('mode')}): {result.get('title')}"
        )
    else:
        await request.app.state.bus.log(
            None, f"Проблема с таблицей: {result.get('error') or result.get('missing')}", "warn"
        )
    return result


@router.post("/sync")
async def sync_now(request: Request) -> dict:
    result = await request.app.state.sync.cycle()
    if result.get("ok"):
        await request.app.state.bus.log(
            None,
            f"Синхронизация: строк из листа {result.get('pulled')}, "
            f"обновлено в листе {result.get('pushed')}",
        )
    return result
