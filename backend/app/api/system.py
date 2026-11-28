"""REST: состояние, журнал, демо-данные."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@router.get("/stats")
async def stats(request: Request) -> dict:
    return await request.app.state.sup.metrics()


@router.get("/run/state")
async def run_state(request: Request) -> dict:
    return request.app.state.sup.snapshot()


@router.get("/logs/history")
async def logs_history(request: Request) -> dict:
    return {"events": request.app.state.bus.history()}


@router.post("/logs/clear")
async def logs_clear(request: Request) -> dict:
    await request.app.state.bus.clear()
    return {"ok": True}


@router.get("/runs")
async def runs(request: Request, limit: int = 100) -> dict:
    limit = max(1, min(limit, 500))
    rows = await request.app.state.db.recent_runs(limit)
    for row in rows:
        row.pop("steps_json", None)
    return {"items": rows}


@router.post("/dev/reset")
async def reset_demo(request: Request) -> dict:
    """Полный сброс встроенной таблицы: задания, история прогонов."""
    sup = request.app.state.sup
    if sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}

    db = request.app.state.db
    for table in ("sheet_state", "runs", "tasks"):
        await db.execute(f"DELETE FROM {table}")

    await request.app.state.bus.log(None, "Таблица заданий очищена")
    await request.app.state.sync.cycle()
    return {"ok": True}
