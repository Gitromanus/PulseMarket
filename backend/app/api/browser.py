"""REST: состояние браузерного слоя и проверочный запуск (M2)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..browser.profiles import ProfileStore
from ..browser.providers import ProviderError, build_provider, check_chromium
from ..browser.proxies import ProxyAssignments, ProxyPool, parse_proxy
from ..core.config import load_settings
from ..flows.check import run_check

router = APIRouter(prefix="/api/browser", tags=["browser"])


class CheckRequest(BaseModel):
    url: str | None = None


class GenerateProfiles(BaseModel):
    count: int = Field(default=10, ge=1, le=500)


class WarmupStart(BaseModel):
    count: int = Field(default=10, ge=1, le=500)


class ProxyLine(BaseModel):
    line: str = Field(min_length=3, max_length=500)


class PruneRequest(BaseModel):
    include_errors: bool = False


def _proxy_path(settings) -> Path | None:
    raw = (settings.general.proxies_file or "").strip()
    return Path(raw) if raw else None


def _proxy_rows(settings, statuses: dict[str, str]) -> dict:
    path = _proxy_path(settings)
    pool = ProxyPool(str(path) if path else None)
    mapping = ProxyAssignments().all()
    active_names = {profile.id for profile in ProfileStore(settings).list("active")}
    counts = Counter(raw for name, raw in mapping.items() if name in active_names)
    items: list[dict] = []
    if path is not None and path.is_file():
        for index, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            proxy = parse_proxy(text)
            items.append(
                {
                    "index": index,
                    "server": proxy.server if proxy else text[:120],
                    "masked": proxy.masked() if proxy else text[:120],
                    "valid": proxy is not None,
                    "status": statuses.get(proxy.raw) if proxy else "invalid",
                    "profiles": counts.get(proxy.raw, 0) if proxy else 0,
                }
            )
    return {
        "file": str(path) if path else "",
        "items": items,
        "total": len(items),
        "invalid": sum(1 for item in items if not item["valid"]),
    }


@router.get("/profiles")
async def profiles(request: Request) -> dict:
    """Профили: счётчики, папки и закреплённый за каждым прокси."""
    settings = load_settings()
    store = ProfileStore(settings)
    state = store.state()
    pool = ProxyPool(settings.general.proxies_file or None)
    mapping = ProxyAssignments().all()
    statuses = request.app.state.proxy_check.results_by_raw()
    items = []
    for profile in store.list("active"):
        raw = mapping.get(profile.id)
        proxy = pool.find(raw)
        items.append(
            {
                "name": profile.label,
                "new": profile.is_new,
                "proxy": proxy.masked() if proxy else None,
                "proxy_status": statuses.get(proxy.raw) if proxy else None,
                "proxy_missing": bool(raw and proxy is None),
            }
        )
    state["items"] = items
    return state


@router.post("/profiles/generate")
async def generate_profiles(payload: GenerateProfiles, request: Request) -> dict:
    """Создаёт пустые профили в папке «Активные» и выдаёт им отпечатки."""
    settings = load_settings()
    store = ProfileStore(settings)
    try:
        created = store.create_many(payload.count)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    await request.app.state.bus.log(
        None,
        f"Создано профилей: {len(created)} ({created[0].label} … {created[-1].label})",
        "success",
    )
    return {
        "ok": True,
        "created": len(created),
        "names": [profile.label for profile in created],
        "profiles": store.state(),
    }


@router.get("/warmup")
async def warmup_status(request: Request) -> dict:
    return request.app.state.warmup.status()


@router.post("/warmup/start")
async def warmup_start(payload: WarmupStart, request: Request) -> dict:
    supervisor = request.app.state.sup
    if supervisor.state != "idle":
        return {
            "ok": False,
            "error": "Сначала остановите прогон: прогрев и очередь одновременно не запускаются",
        }
    busy = supervisor.busy_profile_paths() | request.app.state.manual.busy_paths()
    return await request.app.state.warmup.start(
        load_settings(), payload.count, busy_paths=busy
    )


@router.post("/warmup/stop")
async def warmup_stop(request: Request) -> dict:
    return await request.app.state.warmup.stop()


@router.get("/proxies")
async def proxies_list(request: Request) -> dict:
    settings = load_settings()
    return _proxy_rows(settings, request.app.state.proxy_check.results_by_raw())


@router.post("/proxies")
async def proxy_add(payload: ProxyLine, request: Request) -> dict:
    line = payload.line.strip()
    if "\n" in line or "\r" in line:
        return {"ok": False, "error": "Одна строка = один прокси"}
    if parse_proxy(line) is None:
        return {"ok": False, "error": "Не удалось разобрать прокси: нужен host:port или схема://user:pass@host:port"}
    settings = load_settings()
    path = _proxy_path(settings)
    if path is None:
        return {"ok": False, "error": "Сначала укажите файл прокси в настройках"}
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
    if any(existing.strip() == line for existing in lines):
        return {"ok": False, "error": "Такой прокси уже есть в списке"}
    lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    await request.app.state.bus.log(None, f"Добавлен прокси: {parse_proxy(line).masked()}")
    return {
        "ok": True,
        "line": line,
        **_proxy_rows(settings, request.app.state.proxy_check.results_by_raw()),
    }


@router.delete("/proxies/{index}")
async def proxy_delete(index: int, request: Request) -> dict:
    settings = load_settings()
    path = _proxy_path(settings)
    if path is None or not path.is_file():
        return {"ok": False, "error": "Файл прокси не найден"}
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if index < 0 or index >= len(lines):
        return {"ok": False, "error": "Строка не найдена — обновите список"}
    removed = lines.pop(index)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proxy = parse_proxy(removed)
    await request.app.state.bus.log(
        None, f"Удалён прокси: {proxy.masked() if proxy else removed[:80]}", "warn"
    )
    return {
        "ok": True,
        "removed": removed.strip(),
        **_proxy_rows(settings, request.app.state.proxy_check.results_by_raw()),
    }


@router.post("/proxies/assign")
async def proxies_assign(request: Request) -> dict:
    """Дозакрепляет за профилями прокси, у которых закрепления ещё нет."""
    settings = load_settings()
    path = _proxy_path(settings)
    pool = ProxyPool(str(path) if path else None)
    if pool.size == 0:
        return {"ok": False, "error": "Нет прокси: добавьте адреса в список"}
    assignments = ProxyAssignments()
    active = ProfileStore(settings).list("active")
    assigned = 0
    for profile in active:
        if pool.find(assignments.get(profile.id)) is None:
            assignments.proxy_for(profile.id, pool)
            assigned += 1
    await request.app.state.bus.log(
        None, f"Закреплено прокси за профилями: {assigned} из {len(active)}", "success"
    )
    return {
        "ok": True,
        "assigned": assigned,
        "profiles": len(active),
        **_proxy_rows(settings, request.app.state.proxy_check.results_by_raw()),
    }


@router.post("/proxies/rebalance")
async def proxies_rebalance(request: Request) -> dict:
    """Заново раскладывает все активные профили по пулу (меняет им IP)."""
    settings = load_settings()
    path = _proxy_path(settings)
    pool = ProxyPool(str(path) if path else None)
    if pool.size == 0:
        return {"ok": False, "error": "Нет прокси: добавьте адреса в список"}
    active = ProfileStore(settings).list("active")
    if not active:
        return {"ok": False, "error": "Нет активных профилей"}
    assigned = ProxyAssignments().rebalance([profile.id for profile in active], pool)
    await request.app.state.bus.log(
        None,
        f"Перераспределено профилей по прокси: {assigned} на {pool.size} адресов",
        "warn",
    )
    return {
        "ok": True,
        "assigned": assigned,
        "profiles": len(active),
        **_proxy_rows(settings, request.app.state.proxy_check.results_by_raw()),
    }


@router.get("/proxies/check")
async def proxy_check_status(request: Request) -> dict:
    return request.app.state.proxy_check.status()


@router.post("/proxies/check/start")
async def proxy_check_start(request: Request) -> dict:
    supervisor = request.app.state.sup
    if supervisor.state != "idle":
        return {"ok": False, "error": "Сначала остановите прогон: проверка прокси идёт отдельно"}
    if request.app.state.warmup.status().get("running"):
        return {"ok": False, "error": "Сначала дождитесь окончания прогрева профилей"}
    return await request.app.state.proxy_check.start(load_settings())


@router.post("/proxies/check/stop")
async def proxy_check_stop(request: Request) -> dict:
    return await request.app.state.proxy_check.stop()


@router.post("/proxies/prune")
async def proxy_prune(request: Request, payload: PruneRequest | None = None) -> dict:
    return await request.app.state.proxy_check.prune_blocked(
        include_errors=bool(payload and payload.include_errors)
    )


@router.get("/status")
async def status() -> dict:
    settings = load_settings()
    chromium = await check_chromium()
    store = ProfileStore(settings)
    pool = ProxyPool(settings.general.proxies_file or None)
    return {
        "provider": settings.browser.provider,
        "headless": settings.browser.headless,
        "enabled": settings.browser.enabled,
        "check_url": settings.browser.check_url,
        "chromium": chromium,
        "profiles": store.state(),
        "proxies": pool.state(),
    }


@router.post("/check")
async def check(request: Request, payload: CheckRequest | None = None) -> dict:
    settings = load_settings()
    url = (payload.url if payload else None) or settings.browser.check_url
    store = ProfileStore(settings)
    profile, _ = store.acquire(0)  # всегда безакк, чтобы не жечь аккаунт
    provider = build_provider(settings.browser)
    session = None
    try:
        session = await provider.open(profile, None)
        result = await run_check(session, url, request.app.state.bus, 0, settings)
        return {
            "ok": True,
            "title": result.get("title"),
            "http": result.get("status"),
            "url": result.get("url"),
            **session.info(),
        }
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if session is not None:
            await provider.close(session)
