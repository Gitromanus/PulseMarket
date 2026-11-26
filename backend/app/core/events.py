"""Шина событий: один источник для WebSocket-подписчиков и истории логов."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any


class EventBus:
    def __init__(self, history_size: int = 400) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "log":
            self._history.append({"type": "log", "payload": payload})
            if len(self._history) > 300:
                keep = sum(1 for e in self._history if e["type"] == "log")
                while keep > 300:
                    self._history.popleft()
                    keep -= 1
        envelope = {"type": event_type, "payload": payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # Медленный клиент не должен вешать движок.
                self._subscribers.discard(queue)
        await asyncio.sleep(0)

    async def clear(self) -> None:
        """Очищает историю и просит все открытые окна очистить лог."""
        self._history.clear()
        await self.publish("log_cleared", {})

    async def log(self, thread: int | None, message: str, level: str = "info") -> None:
        await self.publish(
            "log",
            {
                "ts": datetime.now().strftime("%H:%M:%S"),
                "thread": thread,
                "level": level,
                "msg": message,
            },
        )
