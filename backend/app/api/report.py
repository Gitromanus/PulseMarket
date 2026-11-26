"""REST: планировщик и отчёт о ресурсах."""

from __future__ import annotations

import os
from pathlib import Path

import psutil
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..browser.profiles import ProfileStore
from ..browser.proxies import ProxyPool
from ..core.config import app_data_dir, load_settings, save_settings

router = APIRouter(prefix="/api", tags=["report"])


class SchedulerPatch(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = Field(default=None, ge=5, le=1440)


@router.get("/scheduler")
async def scheduler_state(request: Request) -> dict:
    return request.app.state.scheduler.state()


@router.post("/scheduler")
async def scheduler_update(payload: SchedulerPatch, request: Request) -> dict:
    settings = load_settings()
    if payload.enabled is not None:
        settings.general.schedule_enabled = payload.enabled
    if payload.interval_min is not None:
        settings.general.schedule_interval_min = payload.interval_min
    save_settings(settings)
    scheduler = request.app.state.scheduler
    scheduler.reset()
    scheduler.schedule_next()
    await request.app.state.bus.log(
        None,
        "Планировщик: "
        + ("включён" if settings.general.schedule_enabled else "выключен")
        + f", интервал {settings.general.schedule_interval_min} мин",
    )
    return request.app.state.scheduler.state()


def _dir_size(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    total = 0
    files = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def _mb(value: int) -> float:
    return round(value / (1024 * 1024), 1)


def _chromium_memory() -> tuple[int, float]:
    count = 0
    memory = 0
    try:
        for child in psutil.Process().children(recursive=True):
            try:
                name = (child.name() or "").lower()
                if "chrom" not in name:
                    continue
                count += 1
                memory += child.memory_info().rss
            except (psutil.Error, OSError):
                continue
    except psutil.Error:
        pass
    return count, _mb(memory)


@router.get("/report")
async def report(request: Request) -> dict:
    db = request.app.state.db
    sup = request.app.state.sup
    manual = request.app.state.manual
    settings = load_settings()

    tasks = await db.counts()
    runs = await db.runs_summary()
    db_file = app_data_dir() / "app.db"
    db_size = db_file.stat().st_size if db_file.exists() else 0
    artifacts_size, artifacts_files = _dir_size(app_data_dir() / "artifacts")
    chrome_count, chrome_mb = _chromium_memory()
    backend_mb = _mb(psutil.Process().memory_info().rss)

    profiles = ProfileStore(settings).state()
    proxies = ProxyPool(settings.general.proxies_file or None).state()

    return {
        "tasks": tasks,
        "runs": runs,
        "profiles": profiles,
        "proxies": proxies,
        "sessions": {"threads": len(sup.sessions), "manual": len(manual.items)},
        "processes": {
            "chromium": chrome_count,
            "chromium_mb": chrome_mb,
            "backend_mb": backend_mb,
        },
        "disk": {
            "db_mb": _mb(db_size),
            "artifacts_mb": _mb(artifacts_size),
            "artifacts_files": artifacts_files,
        },
        "scheduler": request.app.state.scheduler.state(),
    }
