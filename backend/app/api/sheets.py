"""REST: состояние встроенной таблицы заданий."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/sheets", tags=["sheets"])


@router.get("/status")
async def status(request: Request) -> dict:
    return request.app.state.sync.status()


@router.post("/sync")
async def sync_now(request: Request) -> dict:
    return await request.app.state.sync.cycle()
