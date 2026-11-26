"""Проверка очереди без pytest: python tests/test_queue.py

Ключевое свойство: 30 параллельных захватов не должны выдать одно задание дважды.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import Database  # noqa: E402


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pf-queue-"))
    db = Database(tmp / "test.db")
    await db.connect()
    try:
        await run_assertions(db)
    finally:
        await db.close()
    print("queue: ok — 20 уникальных захватов, идемпотентное завершение, lease, error-limit")


async def run_assertions(db: Database) -> None:
    for i in range(20):
        await db.conn.execute(
            """
            INSERT INTO tasks (sheet_id, marketplace, sku, query, target_runs)
            VALUES (?, 'ozon', ?, 'тестовый запрос', 2)
            """,
            (str(uuid.uuid4()), f"SKU-{i}"),
        )
    await db.conn.commit()

    claims = await asyncio.gather(*(db.claim_task(f"thread-{i}") for i in range(30)))
    taken = [c["id"] for c in claims if c]

    assert len(taken) == 20, f"захвачено {len(taken)} из 20"
    assert len(set(taken)) == 20, "одно задание захвачено дважды"

    first = next(c for c in claims if c)
    await db.complete_task(first["id"], position=17, cart=1, likes=1)
    updated = await db.conn.execute(
        "SELECT status, done_runs, start_position, cart_adds FROM tasks WHERE id = ?",
        (first["id"],),
    )
    row = await updated.fetchone()
    assert row["status"] == "pending" and row["done_runs"] == 1, dict(row)
    assert row["start_position"] == 17 and row["cart_adds"] == 1, dict(row)

    await db.complete_task(first["id"], position=9, cart=0, likes=0)
    row = (await (await db.conn.execute(
        "SELECT status, done_runs, position, start_position FROM tasks WHERE id = ?",
        (first["id"],),
    )).fetchone())
    assert row["status"] == "done" and row["position"] == 9, dict(row)
    assert row["start_position"] == 17, "начальная позиция не должна перезаписываться"

    # Просроченный lease возвращается в очередь.
    expired = (await (await db.conn.execute(
        "SELECT COUNT(1) AS c FROM tasks WHERE status='claimed'"
    )).fetchone())["c"]
    assert expired == 19, f"ожидали 19 захваченных, получили {expired}"
    await db.conn.execute(
        "UPDATE tasks SET lease_until=datetime('now','-1 minute') WHERE status='claimed'"
    )
    await db.conn.commit()
    released = await db.release_expired()
    assert released == expired, f"освобождено {released} из {expired}"

    # Повторные сбои переводят задание в error (3 сбоя).
    claimed = await db.claim_task("thread-x")
    assert claimed
    await db.fail_task(claimed["id"], "boom")
    row = (await (await db.conn.execute(
        "SELECT status, attempts FROM tasks WHERE id=?", (claimed["id"],)
    )).fetchone())
    assert row["status"] == "pending" and row["attempts"] == 1, dict(row)

    await db.claim_task("thread-x")
    await db.fail_task(claimed["id"], "boom")
    await db.claim_task("thread-x")
    await db.fail_task(claimed["id"], "boom")
    row = (await (await db.conn.execute(
        "SELECT status, attempts FROM tasks WHERE id=?", (claimed["id"],)
    )).fetchone())
    assert row["status"] == "error", dict(row)

    # Мягкая остановка не должна превращать задание в error.
    other = await db.claim_task("thread-y")
    assert other and other["id"] != claimed["id"]
    await db.release_task(other["id"])
    row = (await (await db.conn.execute(
        "SELECT status, attempts FROM tasks WHERE id=?", (other["id"],)
    )).fetchone())
    assert row["status"] == "pending", dict(row)


if __name__ == "__main__":
    asyncio.run(scenario())
