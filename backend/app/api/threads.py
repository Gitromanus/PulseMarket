"""REST: управление отдельными потоками (он же будущий режим ручного управления)."""

from __future__ import annotations

import io

from fastapi import APIRouter, Request, Response
from PIL import Image, ImageDraw
from pydantic import BaseModel

from .. import human

router = APIRouter(prefix="/api/threads", tags=["threads"])


def _draw_cursor(
    data: bytes, position: tuple[float, float], viewport: dict | None
) -> bytes:
    """Рисует виртуальный курсор на кадре превью: сам браузер его не показывает."""
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return data
    width = int((viewport or {}).get("width") or image.width)
    height = int((viewport or {}).get("height") or image.height)
    if width <= 0 or height <= 0:
        return data
    scale_x = image.width / width
    scale_y = image.height / height
    x = min(max(position[0], 0.0), width - 1) * scale_x
    y = min(max(position[1], 0.0), height - 1) * scale_y
    size = max(14.0, min(26.0, image.width / 80))
    arrow = [
        (0, 0),
        (0, 18),
        (4.5, 13.5),
        (7.5, 21),
        (10.5, 19.5),
        (7.5, 12),
        (13, 12),
    ]
    points = [(x + px * size / 16.0, y + py * size / 16.0) for px, py in arrow]
    draw = ImageDraw.Draw(image)
    draw.polygon(points, fill=(255, 255, 255))
    draw.line(points + [points[0]], fill=(0, 0, 0), width=max(1, round(size / 9)))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=55)
    return out.getvalue()


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
    cursor = human.cursor(session.page)
    if cursor is not None:
        data = _draw_cursor(data, cursor, session.page.viewport_size)
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
