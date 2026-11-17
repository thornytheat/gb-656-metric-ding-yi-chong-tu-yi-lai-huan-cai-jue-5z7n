"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_sessions (
    merge_id            TEXT PRIMARY KEY,
    merge_key           TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,
    base_ref            TEXT NOT NULL,
    left_ref            TEXT NOT NULL,
    right_ref           TEXT NOT NULL,
    base_digest         TEXT NOT NULL,
    left_digest         TEXT NOT NULL,
    right_digest        TEXT NOT NULL,
    content_digest      TEXT NOT NULL,
    selections_json     TEXT NOT NULL,
    selection_revision  INTEGER NOT NULL,
    options_json        TEXT NOT NULL,
    conflicts_json      TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    pending_json        TEXT,
    published_run_id    TEXT,
    published_version_seq INTEGER,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
"""


def db_path() -> Path:
    raw = os.environ.get("METRICGRAPH_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv", "merge_sessions"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> dict:
    return insert_run_if_absent(run)


def insert_run_if_absent(run: dict) -> dict:
    """插入批次；批次键已存在则原样返回已有记录，绝不覆盖、绝不重复计数。

    版本序号也在同一把锁里取，两个并发插入不会拿到同一个序号。
    """
    with _LOCK, _connect() as conn:
        existing = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (run["batch_key"],)
        ).fetchone()
        if existing is not None:
            return _loads(existing["payload_json"])
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs"
        ).fetchone()
        stored = dict(run)
        stored["version_seq"] = int(seq_row["n"]) + 1
        summary = dict(stored.get("summary") or {})
        summary["version_seq"] = stored["version_seq"]
        stored["summary"] = summary
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stored["run_id"], stored["batch_key"], stored["old_schema_id"],
                stored["new_schema_id"], int(stored["version_seq"]),
                stored["input_sha256"], stored["ruleset"], stored["created_at"],
                stored["effective_from"], stored["effective_to"],
                _dumps(summary), _dumps(stored),
            ),
        )
    return stored


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 合并会话 ----------

SESSION_PAYLOAD_FIELDS = ("options", "conflicts", "labels", "snapshots", "merge",
                          "recovered")


def save_merge_session(session: dict) -> None:
    payload = {key: session.get(key) for key in SESSION_PAYLOAD_FIELDS
               if session.get(key) is not None}
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO merge_sessions (
                   merge_id, merge_key, status, base_ref, left_ref, right_ref,
                   base_digest, left_digest, right_digest, content_digest,
                   selections_json, selection_revision, options_json, conflicts_json,
                   payload_json, pending_json, published_run_id, published_version_seq,
                   last_error, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(merge_id) DO UPDATE SET
                   status=excluded.status,
                   selections_json=excluded.selections_json,
                   selection_revision=excluded.selection_revision,
                   options_json=excluded.options_json,
                   conflicts_json=excluded.conflicts_json,
                   payload_json=excluded.payload_json,
                   pending_json=excluded.pending_json,
                   published_run_id=excluded.published_run_id,
                   published_version_seq=excluded.published_version_seq,
                   last_error=excluded.last_error,
                   updated_at=excluded.updated_at""",
            (
                session["merge_id"], session["merge_key"], session["status"],
                session["base_ref"], session["left_ref"], session["right_ref"],
                session["base_digest"], session["left_digest"], session["right_digest"],
                session["content_digest"],
                _dumps(session.get("selections") or {}),
                int(session.get("selection_revision") or 0),
                _dumps(session.get("options") or {}),
                _dumps(session.get("conflicts") or []),
                _dumps(payload),
                _dumps(session.get("pending")) if session.get("pending") else None,
                session.get("published_run_id"),
                session.get("published_version_seq"),
                session.get("last_error"),
                session["created_at"], session["updated_at"],
            ),
        )


def _row_to_session(row) -> dict:
    payload = _loads(row["payload_json"]) or {}
    session = {
        "merge_id": row["merge_id"],
        "merge_key": row["merge_key"],
        "status": row["status"],
        "base_ref": row["base_ref"],
        "left_ref": row["left_ref"],
        "right_ref": row["right_ref"],
        "base_digest": row["base_digest"],
        "left_digest": row["left_digest"],
        "right_digest": row["right_digest"],
        "content_digest": row["content_digest"],
        "selections": _loads(row["selections_json"]) or {},
        "selection_revision": int(row["selection_revision"]),
        "options": _loads(row["options_json"]) or {},
        "conflicts": _loads(row["conflicts_json"]) or [],
        "pending": _loads(row["pending_json"]),
        "published_run_id": row["published_run_id"],
        "published_version_seq": row["published_version_seq"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    for key in ("labels", "snapshots", "merge"):
        session[key] = payload.get(key) or {}
    if payload.get("recovered"):
        session["recovered"] = True
    return session


def mark_merge_published(merge_id: str, run_id: str, version_seq: int,
                         updated_at: str, recovered: bool = False) -> None:
    """只更新发布结果，不动选择与版本号，避免覆盖并发写入。"""
    with _LOCK, _connect() as conn:
        if recovered:
            row = conn.execute(
                "SELECT payload_json FROM merge_sessions WHERE merge_id=?",
                (merge_id,),
            ).fetchone()
            payload = (_loads(row["payload_json"]) if row else None) or {}
            payload["recovered"] = True
            conn.execute(
                """UPDATE merge_sessions
                      SET status='published', published_run_id=?,
                          published_version_seq=?, last_error=NULL, pending_json=NULL,
                          payload_json=?, updated_at=?
                    WHERE merge_id=?""",
                (run_id, int(version_seq), _dumps(payload), updated_at, merge_id),
            )
            return
        conn.execute(
            """UPDATE merge_sessions
                  SET status='published', published_run_id=?, published_version_seq=?,
                      last_error=NULL, pending_json=NULL, updated_at=?
                WHERE merge_id=?""",
            (run_id, int(version_seq), updated_at, merge_id),
        )


def mark_merge_failure(merge_id: str, message: str, updated_at: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE merge_sessions SET last_error=?, updated_at=? WHERE merge_id=?",
            (message, updated_at, merge_id),
        )


def load_merge_session(merge_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM merge_sessions WHERE merge_id=?", (merge_id,)
        ).fetchone()
    return _row_to_session(row) if row else None


def update_merge_selections(merge_id: str, selections: dict, expected_revision: int,
                            updated_at: str) -> dict | None:
    """原子写入裁决：只有版本号仍等于 expected_revision 才推进。

    返回更新后的会话；版本号已被别的写入推进时返回 None，由调用方拒绝。
    """
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """UPDATE merge_sessions
                  SET selections_json=?, selection_revision=selection_revision+1,
                      pending_json=NULL, updated_at=?
                WHERE merge_id=? AND selection_revision=?""",
            (_dumps(selections), updated_at, merge_id, int(expected_revision)),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM merge_sessions WHERE merge_id=?", (merge_id,)
        ).fetchone()
    return _row_to_session(row) if row else None


def claim_merge_publish(merge_id: str, expected_revision: int | None,
                        pending: dict, updated_at: str,
                        allow_pending: bool = False) -> bool:
    """发布前原子认领：版本号仍是这次读到的那个、且没有别人正在发布，才落标记。

    另一个发布已经落了「未完成」标记时直接失败，避免同一会话并发发布两次；
    写入失败后的恢复走 ``allow_pending``，沿用原标记接着做。
    """
    guard = "" if allow_pending else " AND pending_json IS NULL"
    with _LOCK, _connect() as conn:
        if expected_revision is None:
            cursor = conn.execute(
                f"""UPDATE merge_sessions SET pending_json=?, updated_at=?
                     WHERE merge_id=?{guard}""",
                (_dumps(pending), updated_at, merge_id),
            )
        else:
            cursor = conn.execute(
                f"""UPDATE merge_sessions SET pending_json=?, updated_at=?
                     WHERE merge_id=? AND selection_revision=?{guard}""",
                (_dumps(pending), updated_at, merge_id, int(expected_revision)),
            )
        return cursor.rowcount > 0


def find_merge_session_by_key(merge_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM merge_sessions WHERE merge_key=?", (merge_key,)
        ).fetchone()
    return _row_to_session(row) if row else None


def list_merge_sessions(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT merge_id, merge_key, status, base_ref, left_ref, right_ref,
                      content_digest, selection_revision, published_run_id,
                      published_version_seq, last_error, created_at, updated_at,
                      conflicts_json
               FROM merge_sessions ORDER BY created_at DESC, merge_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "merge_id": row["merge_id"],
            "merge_key": row["merge_key"],
            "status": row["status"],
            "base_ref": row["base_ref"],
            "left_ref": row["left_ref"],
            "right_ref": row["right_ref"],
            "content_digest": row["content_digest"],
            "selection_revision": int(row["selection_revision"]),
            "published_run_id": row["published_run_id"],
            "published_version_seq": row["published_version_seq"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "conflict_count": len(_loads(row["conflicts_json"]) or []),
        }
        for row in rows
    ]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default
