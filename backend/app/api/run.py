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
    """Возвращает выполненные задания текущего листа в очередь (повторный прогон)."""
    if request.app.state.sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}

    db = request.app.state.db
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'sync_source'")
    source = row["value"] if row else None
    if not source:
        return {"ok": False, "error": "Источник таблицы не определён: сначала синхронизируйте лист"}

    tasks = await db.fetchall(
        "SELECT id FROM tasks WHERE source = ? AND status IN ('done', 'error')", (source,)
    )
    if not tasks:
        return {"ok": False, "error": "Нет выполненных заданий для повтора"}

    await db.execute(
        "UPDATE tasks SET done_runs = 0, status = 'pending', last_error = NULL,"
        " updated_at = datetime('now')"
        " WHERE source = ? AND status IN ('done', 'error')",
        (source,),
    )
    for task in tasks:
        await db.execute("DELETE FROM sheet_state WHERE task_id = ?", (task["id"],))

    await request.app.state.bus.log(
        None, f"Возвращено в очередь выполненных заданий: {len(tasks)}"
    )
    return {"ok": True, "requeued": len(tasks)}
