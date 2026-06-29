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

                CREATE TABLE IF NOT EXISTS team_members (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id   INTEGER NOT NULL,
                    username   TEXT NOT NULL,
                    user_id    INTEGER,
                    name       TEXT DEFAULT '',
                    role       TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(owner_id, username)
                );

                CREATE INDEX IF NOT EXISTS idx_members_owner    ON team_members(owner_id);
                CREATE INDEX IF NOT EXISTS idx_members_username ON team_members(username);
                CREATE INDEX IF NOT EXISTS idx_members_user_id  ON team_members(user_id);

                CREATE TABLE IF NOT EXISTS assignments (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id       INTEGER NOT NULL,
                    member_id      INTEGER NOT NULL,
                    title          TEXT NOT NULL,
                    note           TEXT DEFAULT '',
                    scheduled_at   TEXT NOT NULL,
                    status         TEXT DEFAULT 'pending',
                    decline_reason TEXT,
                    sent_flag      INTEGER DEFAULT 0,
                    created_at     TEXT DEFAULT (datetime('now')),
                    updated_at     TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (owner_id)  REFERENCES users(id),
                    FOREIGN KEY (member_id) REFERENCES team_members(id)
                );

                CREATE INDEX IF NOT EXISTS idx_assign_owner  ON assignments(owner_id);
                CREATE INDEX IF NOT EXISTS idx_assign_member ON assignments(member_id);
                CREATE INDEX IF NOT EXISTS idx_assign_status ON assignments(status);
                CREATE INDEX IF NOT EXISTS idx_assign_sched  ON assignments(scheduled_at);
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

    def get_tasks_in_range_all_users(self, dt_from: str, dt_to: str) -> list:
        """Задачи всех пользователей в диапазоне времени."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status='pending'
                   AND scheduled_at BETWEEN ? AND ?
                   ORDER BY scheduled_at ASC""",
                (dt_from, dt_to),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Team members ────────────────────────────────────────────────────────────

    def add_member(self, owner_id: int, username: str, name: str = "", role: str = "") -> int:
        username = username.lstrip("@").lower()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO team_members(owner_id, username, name, role)
                   VALUES (?, ?, ?, ?)""",
                (owner_id, username, name, role),
            )
            if cur.lastrowid:
                return cur.lastrowid
            row = conn.execute(
                "SELECT id FROM team_members WHERE owner_id=? AND username=?",
                (owner_id, username),
            ).fetchone()
            return row["id"] if row else None

    def get_members(self, owner_id: int) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM team_members WHERE owner_id=? ORDER BY name ASC",
                (owner_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_member(self, member_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM team_members WHERE id=?", (member_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_member_by_username(self, owner_id: int, username: str) -> Optional[Dict]:
        username = username.lstrip("@").lower()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM team_members WHERE owner_id=? AND username=?",
                (owner_id, username),
            ).fetchone()
            return dict(row) if row else None

    def link_member_user_id(self, username: str, user_id: int, name: str = ""):
        """Привязывает Telegram user_id к участнику команды по username."""
        username = username.lstrip("@").lower()
        with self._conn() as conn:
            conn.execute(
                """UPDATE team_members SET user_id=?, name=CASE WHEN name='' THEN ? ELSE name END
                   WHERE username=?""",
                (user_id, name, username),
            )

    def update_member(self, member_id: int, name: str, role: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE team_members SET name=?, role=? WHERE id=?",
                (name, role, member_id),
            )

    def delete_member(self, member_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM assignments WHERE member_id=?", (member_id,))
            conn.execute("DELETE FROM team_members WHERE id=?", (member_id,))

    # ── Assignments ─────────────────────────────────────────────────────────────

    def add_assignment(
        self,
        owner_id: int,
        member_id: int,
        title: str,
        note: str = "",
        scheduled_at: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO assignments(owner_id, member_id, title, note, scheduled_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (owner_id, member_id, title, note, scheduled_at),
            )
            return cur.lastrowid

    def get_assignment(self, assignment_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM assignments WHERE id=?", (assignment_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_assignments_for_owner(self, owner_id: int) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT a.*, m.name as member_name, m.role as member_role,
                          m.username as member_username
                   FROM assignments a
                   JOIN team_members m ON a.member_id = m.id
                   WHERE a.owner_id=?
                   ORDER BY a.scheduled_at ASC""",
                (owner_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_assignments_for_member(self, member_id: int) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM assignments WHERE member_id=? ORDER BY scheduled_at ASC",
                (member_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_assignment_status(
        self,
        assignment_id: int,
        status: str,
        decline_reason: str = None,
    ):
        with self._conn() as conn:
            conn.execute(
                """UPDATE assignments
                   SET status=?, decline_reason=?, updated_at=datetime('now')
                   WHERE id=?""",
                (status, decline_reason, assignment_id),
            )

    def mark_assignment_sent(self, assignment_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE assignments SET sent_flag=1, updated_at=datetime('now') WHERE id=?",
                (assignment_id,),
            )

    def delete_assignment(self, assignment_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))

    def get_pending_assignments_to_send(self) -> List[Dict]:
        """Возвращает задания, которые ещё не отправлены и чьё время пришло."""
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT a.*, m.user_id as member_user_id, m.name as member_name,
                          m.role as member_role, m.username as member_username
                   FROM assignments a
                   JOIN team_members m ON a.member_id = m.id
                   WHERE a.status='pending'
                     AND a.sent_flag=0
                     AND a.scheduled_at <= ?
                   ORDER BY a.scheduled_at ASC""",
                (now_str,),
            ).fetchall()
            return [dict(r) for r in rows]
