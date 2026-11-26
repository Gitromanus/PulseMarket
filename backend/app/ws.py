"""WebSocket: поток событий для UI."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    app = websocket.app
    token = websocket.query_params.get("token")
    if token != app.state.token:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    bus = app.state.bus
    queue = bus.subscribe()
    try:
        await websocket.send_json({"type": "history", "payload": bus.history()})
        await websocket.send_json(
            {"type": "run_state", "payload": {"state": app.state.sup.state}}
        )
        while True:
            envelope = await queue.get()
            await websocket.send_json(envelope)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        bus.unsubscribe(queue)
