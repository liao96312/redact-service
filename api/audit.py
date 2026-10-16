"""T3.4 审计日志（SQLite）与任务状态存储。

禁止记录任何明文敏感内容，只记录类型、数量、路径、耗时、操作人。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.config import cfg, rel

DB_PATH = rel(cfg("paths", "audit_db", default="/data/audit.db"))
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY,
                filename TEXT,
                kind TEXT,
                status TEXT,
                counts TEXT,
                warnings TEXT,
                operator TEXT,
                enc_path TEXT,
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                filename TEXT,
                kind TEXT,
                status TEXT,
                counts TEXT,
                duration_ms INTEGER,
                operator TEXT,
                ts TEXT
            )"""
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_task(task_id: str, filename: str, kind: str, enc_path: str, operator: str = "") -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tasks(task_id,filename,kind,status,counts,warnings,operator,enc_path,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, filename, kind, "queued", "{}", "[]", operator, enc_path, _now(), _now()),
        )


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        if k in ("counts", "warnings") and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(_now())
    vals.append(task_id)
    with _lock, _conn() as c:
        c.execute(f"UPDATE tasks SET {','.join(cols)} WHERE task_id=?", vals)


def get_task(task_id: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("counts", "warnings"):
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except json.JSONDecodeError:
            d[k] = {} if k == "counts" else []
    return d


def list_review() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT task_id,filename,status,updated_at FROM tasks WHERE status IN ('needs_review','failed') ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_audit(task_id: str, filename: str, kind: str, status: str, counts: dict, duration_ms: int, operator: str = "") -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO audit(task_id,filename,kind,status,counts,duration_ms,operator,ts) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, filename, kind, status, json.dumps(counts, ensure_ascii=False), duration_ms, operator, _now()),
        )
