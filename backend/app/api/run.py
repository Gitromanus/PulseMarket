"""REST: управление прогоном."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/run", tags=["run"])


class StartRequest(BaseModel):
    threads: int | None = Field(default=None, ge=1, le=64)


@router.get("")
async def state(request: Request) -> dict:
    return request.app.state.sup.snapshot()


@router.post("/start")
async def start(request: Request, payload: StartRequest | None = None) -> dict:
    sup = request.app.state.sup
    result = await sup.start(payload.threads if payload else None)
    if not result.get("ok"):
        await request.app.state.bus.log(None, result.get("error", "Не удалось запустить"), "error")
    return result


@router.post("/stop")
async def stop(request: Request) -> dict:
    return await request.app.state.sup.stop()


@router.post("/restart")
async def restart(request: Request, payload: StartRequest | None = None) -> dict:
    return await request.app.state.sup.restart(payload.threads if payload else None)


@router.post("/requeue")
async def requeue(request: Request) -> dict:
    """Возвращает выполненные и ошибочные задания в очередь (повторный прогон)."""
    if request.app.state.sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}

    db = request.app.state.db
    count = await db.requeue_all_finished()
    if not count:
        return {"ok": False, "error": "Нет выполненных заданий для повтора"}

    await request.app.state.bus.log(None, f"Возвращено в очередь заданий: {count}")
    return {"ok": True, "requeued": count}
