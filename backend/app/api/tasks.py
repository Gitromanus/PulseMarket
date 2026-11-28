"""REST: встроенная таблица заданий (без Excel и Google Sheets)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

SKU_RE = re.compile(r"/product/[^/]*?(\d{6,})/")


class TaskPayload(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    sku: str = Field(default="", max_length=64)
    url: str = Field(default="", max_length=1000)
    geo: str = Field(default="", max_length=500)
    target_runs: int = Field(default=1, ge=1, le=1000)
    priority: int = Field(default=0, ge=-100, le=100)
    marketplace: str = Field(default="ozon", max_length=32)

    @model_validator(mode="after")
    def _fill_sku(self) -> "TaskPayload":
        self.query = self.query.strip()
        self.sku = self.sku.strip()
        self.url = self.url.strip()
        if not self.sku and self.url:
            match = SKU_RE.search(self.url)
            if match:
                self.sku = match.group(1)
        if not self.sku:
            raise ValueError("Укажите SKU или ссылку на товар Ozon")
        return self


class TaskPatch(BaseModel):
    query: str | None = Field(default=None, min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=1000)
    geo: str | None = Field(default=None, max_length=500)
    target_runs: int | None = Field(default=None, ge=1, le=1000)
    priority: int | None = Field(default=None, ge=-100, le=100)
    marketplace: str | None = Field(default=None, max_length=32)


class PausePayload(BaseModel):
    paused: bool


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "steps_json"}


@router.get("")
async def list_tasks(request: Request, limit: int = 300, offset: int = 0, search: str = "") -> dict:
    db = request.app.state.db
    limit = max(1, min(limit, 1000))
    items = await db.list_tasks(limit, max(0, offset), search.strip())
    return {
        "items": [_clean(item) for item in items],
        "counts": await db.counts(),
        "progress": await db.progress(),
    }


@router.post("")
async def create_task(payload: TaskPayload, request: Request) -> dict:
    db = request.app.state.db
    try:
        task = await db.create_task(**payload.model_dump())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    await request.app.state.bus.log(
        None, f"Добавлено задание #{task['id']}: {task['query']} ({task['sku']})"
    )
    await request.app.state.bus.publish("tasks_changed", {"action": "create", "id": task["id"]})
    return {"ok": True, "task": _clean(task)}


@router.put("/{task_id}")
async def update_task(task_id: int, payload: TaskPatch, request: Request) -> dict:
    db = request.app.state.db
    values = {key: value for key, value in payload.model_dump().items() if value is not None}
    if "query" in values:
        values["query"] = values["query"].strip()
    if values.get("sku") is not None:
        values["sku"] = values["sku"].strip()
    task = await db.update_task(task_id, values)
    if task is None:
        return {"ok": False, "error": "Задание не найдено"}
    await request.app.state.bus.log(None, f"Изменено задание #{task_id}: {task['query']}")
    await request.app.state.bus.publish("tasks_changed", {"action": "update", "id": task_id})
    return {"ok": True, "task": _clean(task)}


@router.delete("/{task_id}")
async def delete_task(task_id: int, request: Request) -> dict:
    removed = await request.app.state.db.delete_task(task_id)
    if not removed:
        return {"ok": False, "error": "Задание не найдено"}
    await request.app.state.bus.log(None, f"Удалено задание #{task_id} вместе с историей")
    await request.app.state.bus.publish("tasks_changed", {"action": "delete", "id": task_id})
    return {"ok": True}


@router.post("/queue/clear")
async def clear_queue(request: Request) -> dict:
    """Убирает из очереди все ещё не взятые задания (переводит на паузу)."""
    if request.app.state.sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}
    count = await request.app.state.db.pause_pending()
    if not count:
        return {"ok": False, "error": "Очередь уже пуста"}
    await request.app.state.bus.log(None, f"Из очереди убрано заданий: {count} (на паузе)")
    await request.app.state.bus.publish("tasks_changed", {"action": "clear-queue"})
    return {"ok": True, "paused": count}


@router.post("/queue/resume")
async def resume_queue(request: Request) -> dict:
    """Возвращает в очередь задания на паузе и с ошибками."""
    if request.app.state.sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}
    count = await request.app.state.db.resume_all_paused()
    if not count:
        return {"ok": False, "error": "Нет заданий на паузе или с ошибками"}
    await request.app.state.bus.log(None, f"Возвращено в очередь заданий: {count}")
    await request.app.state.bus.publish("tasks_changed", {"action": "resume-queue"})
    return {"ok": True, "resumed": count}


@router.post("/{task_id}/requeue")
async def requeue_task(task_id: int, request: Request) -> dict:
    if request.app.state.sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}
    if not await request.app.state.db.requeue_task(task_id):
        return {"ok": False, "error": "Задание не найдено"}
    await request.app.state.bus.log(None, f"Задание #{task_id} возвращено в очередь")
    await request.app.state.bus.publish("tasks_changed", {"action": "requeue", "id": task_id})
    return {"ok": True}


@router.post("/{task_id}/resume")
async def resume_task(task_id: int, request: Request) -> dict:
    """Возвращает задание в очередь, сохраняя уже выполненные прогоны."""
    if request.app.state.sup.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон"}
    if not await request.app.state.db.resume_task(task_id):
        return {"ok": False, "error": "Задание не найдено"}
    await request.app.state.bus.log(
        None, f"Задание #{task_id} продолжит с текущего прогресса"
    )
    await request.app.state.bus.publish("tasks_changed", {"action": "resume", "id": task_id})
    return {"ok": True}


@router.post("/{task_id}/pause")
async def pause_task(task_id: int, payload: PausePayload, request: Request) -> dict:
    if not await request.app.state.db.set_task_paused(task_id, payload.paused):
        return {"ok": False, "error": "Задание не найдено"}
    state = "на паузе" if payload.paused else "в очереди"
    await request.app.state.bus.log(None, f"Задание #{task_id}: {state}")
    await request.app.state.bus.publish("tasks_changed", {"action": "pause", "id": task_id})
    return {"ok": True}


@router.get("/{task_id}/runs")
async def task_runs(task_id: int, request: Request, limit: int = 100) -> dict:
    rows = await request.app.state.db.task_runs(task_id, max(1, min(limit, 500)))
    return {"items": rows}
