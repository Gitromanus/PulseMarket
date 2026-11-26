"""SQLite: единственный источник истины по очереди заданий."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import aiosqlite

from ..core.config import app_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id             INTEGER PRIMARY KEY,
  sheet_id       TEXT UNIQUE,
  sheet_row      INTEGER,
  marketplace    TEXT NOT NULL,
  sku            TEXT NOT NULL,
  url            TEXT,
  query          TEXT NOT NULL,
  geo            TEXT,
  target_runs    INTEGER NOT NULL DEFAULT 1,
  done_runs      INTEGER NOT NULL DEFAULT 0,
  start_position INTEGER,
  position       INTEGER,
  cart_adds      INTEGER NOT NULL DEFAULT 0,
  likes          INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'pending',
  source         TEXT,
  priority       INTEGER NOT NULL DEFAULT 0,
  attempts       INTEGER NOT NULL DEFAULT 0,
  claim_owner    TEXT,
  claim_at       TEXT,
  lease_until    TEXT,
  last_error     TEXT,
  operator_note  TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_tasks_sheet ON tasks(sheet_id);

CREATE TABLE IF NOT EXISTS profiles (
  id            INTEGER PRIMARY KEY,
  account_id    TEXT UNIQUE,
  phone         TEXT,
  folder        TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'noauth',
  proxy_id      INTEGER,
  state         TEXT NOT NULL DEFAULT 'free',
  uses          INTEGER NOT NULL DEFAULT 0,
  cart_pending  INTEGER NOT NULL DEFAULT 0,
  last_used_at  TEXT,
  note          TEXT
);

CREATE TABLE IF NOT EXISTS proxies (
  id             INTEGER PRIMARY KEY,
  raw            TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL DEFAULT 'resident',
  state          TEXT NOT NULL DEFAULT 'free',
  fail_count     INTEGER NOT NULL DEFAULT 0,
  cooldown_until TEXT,
  last_check_at  TEXT,
  geo            TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY,
  task_id     INTEGER REFERENCES tasks(id),
  thread_id   INTEGER,
  profile_id  INTEGER,
  started_at  TEXT,
  finished_at TEXT,
  result      TEXT,
  position    INTEGER,
  cart_added  INTEGER DEFAULT 0,
  liked       INTEGER DEFAULT 0,
  error_code  TEXT,
  error_msg   TEXT,
  steps_json  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sheet_state (
  task_id   INTEGER PRIMARY KEY,
  payload   TEXT NOT NULL,
  pushed_at TEXT
);
"""

CLAIM_SQL = """
UPDATE tasks
   SET status = 'claimed',
       claim_owner = :owner,
       claim_at = datetime('now'),
       lease_until = datetime('now', :lease),
       updated_at = datetime('now')
 WHERE id = (
   SELECT id FROM tasks
    WHERE status = 'pending'
      AND done_runs < target_runs
    ORDER BY priority DESC, updated_at ASC, id
    LIMIT 1
 )
   AND status = 'pending'
RETURNING *;
"""

COMPLETE_SQL = """
UPDATE tasks
   SET done_runs = done_runs + 1,
       cart_adds = cart_adds + :cart,
       likes = likes + :likes,
       position = :position,
       start_position = COALESCE(start_position, :position),
       status = CASE WHEN done_runs + 1 >= target_runs THEN 'done' ELSE 'pending' END,
       attempts = 0,
       claim_owner = NULL,
       claim_at = NULL,
       lease_until = NULL,
       last_error = NULL,
       updated_at = datetime('now')
 WHERE id = :id
RETURNING *;
"""


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_data_dir() / "app.db")
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Мини-миграции для уже созданных баз (SQLite не умеет ADD COLUMN IF NOT EXISTS)."""
        cur = await self.conn.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cur.fetchall()}
        if "url" not in columns:
            await self.conn.execute("ALTER TABLE tasks ADD COLUMN url TEXT")
        if "source" not in columns:
            await self.conn.execute("ALTER TABLE tasks ADD COLUMN source TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._conn

    # ---------- общий доступ для синхронизации ----------

    async def execute(self, sql: str, params: tuple | dict = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, params)
            await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def claim_task(self, owner: str, lease_minutes: int = 15) -> dict[str, Any] | None:
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.conn.execute(
                    CLAIM_SQL, {"owner": owner, "lease": f"+{lease_minutes} minutes"}
                )
                row = await cur.fetchone()
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        return dict(row) if row else None

    async def release_task(self, task_id: int) -> None:
        """Мягкое освобождение (остановка потока): задание снова в очереди."""
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE tasks
                   SET status = 'pending',
                       claim_owner = NULL,
                       claim_at = NULL,
                       lease_until = NULL,
                       updated_at = datetime('now')
                 WHERE id = :id AND status IN ('claimed', 'running')
                """,
                {"id": task_id},
            )
            await self.conn.commit()

    async def fail_task(self, task_id: int, error: str) -> None:
        """Сбой выполнения: attempts растёт, после 3 сбоев задание уходит в error."""
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE tasks
                   SET attempts = attempts + 1,
                       status = CASE WHEN attempts + 1 >= 3 THEN 'error' ELSE 'pending' END,
                       last_error = :error,
                       claim_owner = NULL,
                       claim_at = NULL,
                       lease_until = NULL,
                       updated_at = datetime('now')
                 WHERE id = :id
                """,
                {"id": task_id, "error": error},
            )
            await self.conn.commit()

    async def last_run_age_minutes(self, task_id: int) -> float | None:
        """Сколько минут прошло с последнего завершения задания (для паузы повтора)."""
        cur = await self.conn.execute(
            "SELECT (julianday('now', 'localtime') - julianday(finished_at)) * 1440.0 AS mins "
            "FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = await cur.fetchone()
        return float(row["mins"]) if row and row["mins"] is not None else None

    async def mark_error(self, task_id: int, error: str) -> None:
        """Терминальная ошибка: строка больше не берётся в работу.

        Для снятых с продажи товаров и подобных случаев, где повтор бессмыслен.
        """
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE tasks
                   SET status = 'error',
                       attempts = MAX(attempts, 3),
                       last_error = :error,
                       claim_owner = NULL,
                       claim_at = NULL,
                       lease_until = NULL,
                       updated_at = datetime('now')
                 WHERE id = :id
                """,
                {"id": task_id, "error": error},
            )
            await self.conn.commit()

    async def complete_task(
        self, task_id: int, position: int | None, cart: int, likes: int
    ) -> dict[str, Any] | None:
        async with self._lock:
            cur = await self.conn.execute(
                COMPLETE_SQL,
                {"id": task_id, "position": position, "cart": cart, "likes": likes},
            )
            row = await cur.fetchone()
            await self.conn.commit()
        return dict(row) if row else None

    async def release_expired(self) -> int:
        async with self._lock:
            cur = await self.conn.execute(
                """
                UPDATE tasks
                   SET status = 'pending', claim_owner = NULL, claim_at = NULL,
                       lease_until = NULL, updated_at = datetime('now')
                 WHERE status IN ('claimed', 'running')
                   AND lease_until < datetime('now')
                """
            )
            await self.conn.commit()
            return cur.rowcount or 0

    async def reset_stale_claims(self) -> int:
        """После жёсткого завершения процесса все claim'ы заведомо мертвы."""
        async with self._lock:
            cur = await self.conn.execute(
                """
                UPDATE tasks
                   SET status = 'pending', claim_owner = NULL, claim_at = NULL,
                       lease_until = NULL, updated_at = datetime('now')
                 WHERE status IN ('claimed', 'running')
                """
            )
            await self.conn.commit()
            return cur.rowcount or 0

    async def count_pending(self) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending' AND done_runs < target_runs"
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def count_unfinished(self) -> int:
        """Задания, которые ещё будут выполняться, включая уже взятые потоком.

        Нужно воркерам: если все задания просто `claimed`, очередь не пуста —
        второй поток должен ждать, а не завершаться.
        """
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks "
            "WHERE status IN ('pending', 'claimed', 'running') AND done_runs < target_runs"
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def counts(self) -> dict[str, int]:
        cur = await self.conn.execute(
            """
            SELECT
              SUM(CASE WHEN status = 'pending' AND done_runs < target_runs THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
              SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
              SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END) AS paused,
              COUNT(*) AS total
            FROM tasks
            """
        )
        row = await cur.fetchone()
        return {k: int(row[k] or 0) for k in ("pending", "done", "errors", "paused", "total")}

    async def record_run(
        self,
        *,
        task_id: int | None,
        thread_id: int | None,
        result: str,
        position: int | None = None,
        cart_added: int = 0,
        liked: int = 0,
        error_code: str | None = None,
        error_msg: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        started_at: str | None = None,
    ) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO runs (task_id, thread_id, started_at, finished_at, result,
                                  position, cart_added, liked, error_code, error_msg, steps_json)
                VALUES (:task_id, :thread_id, COALESCE(:started_at, datetime('now', 'localtime')),
                        datetime('now', 'localtime'), :result,
                        :position, :cart, :liked, :error_code, :error_msg, :steps)
                """,
                {
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "started_at": started_at,
                    "result": result,
                    "position": position,
                    "cart": cart_added,
                    "liked": liked,
                    "error_code": error_code,
                    "error_msg": error_msg,
                    "steps": json.dumps(steps or [], ensure_ascii=False),
                },
            )
            await self.conn.commit()

    async def runs_summary(self) -> dict[str, int]:
        row = await self.fetchone(
            """
            SELECT COUNT(1) AS total,
                   COALESCE(SUM(result = 'ok'), 0) AS ok,
                   COALESCE(SUM(result = 'error'), 0) AS errors,
                   COALESCE(SUM(date(finished_at) = date('now', 'localtime')), 0) AS today
              FROM runs
            """
        )
        return {key: int((row or {}).get(key) or 0) for key in ("total", "ok", "errors", "today")}

    async def recent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT r.*, t.sku, t.query, t.marketplace
              FROM runs r LEFT JOIN tasks t ON t.id = r.task_id
             ORDER BY r.id DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def seed_demo(self, count: int = 6) -> int:
        queries = [
            "беспроводные наушники",
            "термокружка 500 мл",
            "коврик для йоги",
            "органайзер для кухни",
            "ночная лампа usb",
            "рюкзак городской",
        ]
        inserted = 0
        async with self._lock:
            for i in range(count):
                cur = await self.conn.execute(
                    "SELECT 1 FROM tasks WHERE sku = ? AND query = ?",
                    (f"DEMO-{1000 + i}", queries[i % len(queries)]),
                )
                if await cur.fetchone():
                    continue
                await self.conn.execute(
                    """
                    INSERT INTO tasks (marketplace, sku, query, geo, target_runs, priority)
                    VALUES ('ozon', ?, ?, ?, ?, 0)
                    """,
                    (
                        f"DEMO-{1000 + i}",
                        queries[i % len(queries)],
                        "https://www.ozon.ru/city/moskva/",
                        random.randint(2, 4),
                    ),
                )
                inserted += 1
            await self.conn.commit()
        return inserted
