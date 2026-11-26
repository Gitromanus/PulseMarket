"""FastAPI-приложение: локальный API + WebSocket для Electron-UI."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import browser as browser_api
from .api import license as license_api
from .api import manual as manual_api
from .api import report as report_api
from .api import run as run_api
from .api import settings as settings_api
from .api import sheets as sheets_api
from .api import system as system_api
from .api import threads as threads_api
from .browser.providers import shutdown_playwright
from .core.config import write_runtime
from .core.events import EventBus
from .core.license import current_license
from .core.manual import ManualManager
from .core.proxy_check import ProxyCheckManager
from .core.scheduler import Scheduler
from .core.supervisor import Supervisor
from .core.warmup import WarmupManager
from .db.database import Database
from .sheets.sync import SyncEngine
from .ws import router as ws_router


def create_app(port: int | None = None) -> FastAPI:
    port = port or int(os.getenv("PF_PORT", "8765"))
    bus = EventBus()
    db = Database()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        released = await db.reset_stale_claims()
        sync = SyncEngine(db, bus)
        supervisor = Supervisor(db, bus, sync)
        manual = ManualManager(bus)
        warmup = WarmupManager(bus)
        proxy_check = ProxyCheckManager(bus)
        scheduler = Scheduler(supervisor, db, bus)
        app.state.db = db
        app.state.bus = bus
        app.state.sup = supervisor
        app.state.sync = sync
        app.state.manual = manual
        app.state.warmup = warmup
        app.state.proxy_check = proxy_check
        app.state.scheduler = scheduler
        info = write_runtime(port)
        app.state.token = info["token"]
        await bus.log(None, "Ядро запущено, ожидаем команды")
        license_state = current_license()
        if license_state["enabled"]:
            if license_state["valid"]:
                expires = license_state.get("expires") or "бессрочно"
                await bus.log(
                    None,
                    f"Лицензия: {license_state.get('subject') or 'без имени'} · до {expires}"
                    f" · HWID {license_state['hwid']}",
                )
            else:
                await bus.log(
                    None,
                    f"Лицензия не активна: {license_state.get('error')} · HWID {license_state['hwid']}",
                    "error",
                )
        else:
            await bus.log(None, f"Лицензия: режим разработки (без ключа) · HWID {license_state['hwid']}")
        if released:
            await bus.log(None, f"Восстановлено после сбоя: заданий в очереди — {released}")
        sync.start_loop()
        scheduler.start_loop()
        try:
            yield
        finally:
            await scheduler.stop()
            await sync.stop()
            if supervisor.state == "running":
                await supervisor.stop()
            await warmup.stop()
            await proxy_check.stop()
            await manual.close_all()
            await shutdown_playwright()
            await db.close()

    app = FastAPI(title="PulseMarket", version="0.1.0", lifespan=lifespan)
    app.state.token = secrets.token_urlsafe(24)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path.startswith("/api"):
            token = request.headers.get("x-token") or request.query_params.get("token")
            if token != app.state.token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    app.include_router(settings_api.router)
    app.include_router(run_api.router)
    app.include_router(threads_api.router)
    app.include_router(system_api.router)
    app.include_router(sheets_api.router)
    app.include_router(browser_api.router)
    app.include_router(manual_api.router)
    app.include_router(license_api.router)
    app.include_router(report_api.router)
    app.include_router(ws_router)
    return app
