"""REST: состояние, журнал, демо-данные."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..core.config import load_settings
from ..sheets.sync import build_backend

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


@router.post("/dev/seed")
async def seed(request: Request, count: int = 6) -> dict:
    count = max(1, min(count, 50))
    inserted = await request.app.state.db.seed_demo(count)
    await request.app.state.bus.log(
        None, f"Демо-данные: добавлено заданий {inserted}" if inserted else "Демо-данные уже загружены"
    )
    return {"ok": True, "inserted": inserted}


@router.post("/dev/reset")
async def reset_demo(request: Request) -> dict:
    """Сброс тестовых данных: только для фейковой таблицы, только на остановленном прогоне."""
    sup = request.app.state.sup
    if sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}

    backend = build_backend(load_settings())
    if backend.kind != "fake":
        return {"ok": False, "error": "Сброс доступен только для тестовой таблицы"}

    path = getattr(backend, "path", None)
    if path is not None:
        from pathlib import Path

        Path(path).unlink(missing_ok=True)

    db = request.app.state.db
    for table in ("sheet_state", "runs", "tasks"):
        await db.execute(f"DELETE FROM {table}")

    await request.app.state.bus.log(None, "Тестовые данные сброшены, таблица пересоздана")
    await request.app.state.sync.cycle()
    return {"ok": True}
