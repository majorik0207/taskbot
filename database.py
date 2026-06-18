import sqlite3
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any


class Database:
    def __init__(self, path: str = "tasks.db"):
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id         INTEGER PRIMARY KEY,
                    name       TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    title        TEXT NOT NULL,
                    note         TEXT DEFAULT '',
                    scheduled_at TEXT NOT NULL,
                    deadline     TEXT,
                    priority     TEXT DEFAULT 'medium',
                    status       TEXT DEFAULT 'pending',
                    link         TEXT,
                    photo_id     TEXT,
                    is_recurring INTEGER DEFAULT 0,
                    recur_rule   TEXT,
                    created_at   TEXT DEFAULT (datetime('now')),
                    updated_at   TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_user   ON tasks(user_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_sched  ON tasks(scheduled_at);
            """)

    # ── Users ──────────────────────────────────────────────────────────────────

    def ensure_user(self, user_id: int, name: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(id, name) VALUES (?, ?)",
                (user_id, name),
            )

    # ── Tasks ──────────────────────────────────────────────────────────────────

    def add_task(
        self,
        user_id: int,
        title: str,
        note: str = "",
        scheduled_at: str = "",
        deadline: Optional[str] = None,
        priority: str = "medium",
        link: Optional[str] = None,
        photo_id: Optional[str] = None,
        is_recurring: bool = False,
        recur_rule: Optional[str] = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO tasks
                   (user_id, title, note, scheduled_at, deadline, priority,
                    link, photo_id, is_recurring, recur_rule)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user_id, title, note, scheduled_at, deadline, priority,
                 link, photo_id, int(is_recurring), recur_rule),
            )
            return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_user_tasks(
        self,
        user_id: int,
        status: Optional[str] = None,
        priority_filter: Optional[List[str]] = None,
    ) -> List[Dict]:
        with self._conn() as conn:
            q = "SELECT * FROM tasks WHERE user_id=?"
            params: list = [user_id]
            if status:
                q += " AND status=?"
                params.append(status)
            if priority_filter:
                placeholders = ",".join("?" * len(priority_filter))
                q += f" AND priority IN ({placeholders})"
                params.extend(priority_filter)
            q += " ORDER BY scheduled_at ASC"
            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    def get_tasks_range(
        self, user_id: int, date_from, date_to
    ) -> List[Dict]:
        from_str = date_from.isoformat()
        to_str = date_to.isoformat() + "T23:59:59"
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE user_id=? AND status='pending'
                   AND scheduled_at BETWEEN ? AND ?
                   ORDER BY scheduled_at ASC""",
                (user_id, from_str, to_str),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_pending(self) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY scheduled_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_task_status(self, task_id: int, status: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, task_id),
            )

    def update_task_field(self, task_id: int, field: str, value):
        allowed = {"title", "note", "scheduled_at", "deadline",
                   "priority", "link", "photo_id", "status"}
        if field not in allowed:
            raise ValueError(f"Field '{field}' not allowed")
        with self._conn() as conn:
            conn.execute(
                f"UPDATE tasks SET {field}=?, updated_at=datetime('now') WHERE id=?",
                (value, task_id),
            )

    def duplicate_task(self, task_id: int) -> int:
        task = self.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        return self.add_task(
            user_id=task["user_id"],
            title=task["title"] + " (копия)",
            note=task["note"] or "",
            scheduled_at=task["scheduled_at"],
            deadline=task["deadline"],
            priority=task["priority"],
            link=task["link"],
            photo_id=task["photo_id"],
        )

    def delete_task(self, task_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def get_overdue_pending(self) -> List[Dict]:
        """Tasks past their scheduled time that are still pending."""
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status='pending' AND scheduled_at < ?
                   ORDER BY scheduled_at ASC""",
                (now_str,),
            ).fetchall()
            return [dict(r) for r in rows]
