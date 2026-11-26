"""REST: управление отдельными потоками (он же будущий режим ручного управления)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/threads", tags=["threads"])


class BrowserVisibility(BaseModel):
    visible: bool


@router.post("/{thread_id}/browser")
async def browser_visibility(thread_id: int, payload: BrowserVisibility, request: Request) -> dict:
    sup = request.app.state.sup
    result = await sup.set_browser_visible(thread_id, payload.visible)
    if result.get("ok"):
        action = "показан" if payload.visible else "спрятан"
        await request.app.state.bus.log(thread_id, f"Браузер {action} оператором")
    return result


@router.get("/{thread_id}/preview")
async def browser_preview(thread_id: int, request: Request) -> Response:
    """JPEG-скриншот текущей страницы потока для миниокна в UI."""
    sup = request.app.state.sup
    session = sup.sessions.get(thread_id)
    if session is None:
        return Response(status_code=404, content=b"no browser")
    try:
        data = await session.page.screenshot(type="jpeg", quality=45, timeout=5000)
    except Exception as exc:  # noqa: BLE001
        return Response(status_code=503, content=str(exc)[:200].encode("utf-8"))
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/{thread_id}/close")
async def close_thread(thread_id: int) -> dict:
    return {
        "ok": False,
        "error": "Закрытие отдельного потока появится вместе с браузерным слоем (M2)",
    }
