"""SQLite storage for Kommo process-call jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_DB_PATH: Optional[Path] = None


def init_db(db_path: str | Path | None = None) -> None:
    global _DB_PATH
    if db_path is not None:
        _DB_PATH = Path(db_path)
    elif _DB_PATH is None:
        _DB_PATH = Path(__file__).resolve().parent / "kommo_jobs.sqlite"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kommo_call_jobs (
                id TEXT PRIMARY KEY,
                extension TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                lead_id INTEGER,
                upload_source TEXT,
                reason TEXT,
                recording_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kommo_call_jobs_status ON kommo_call_jobs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kommo_call_jobs_extension ON kommo_call_jobs(extension)"
        )
        conn.commit()


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        init_db()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(
    extension: str,
    dedup_key: str,
    payload: dict[str, Any],
    *,
    status: str = "queued",
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = _now_iso()
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO kommo_call_jobs
                    (id, extension, dedup_key, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, extension, dedup_key, json.dumps(payload), status, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM kommo_call_jobs WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if row:
                return _row_to_dict(row)
            raise
    return get_job(job_id) or {}


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kommo_call_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_job_by_dedup(dedup_key: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kommo_call_jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_job(job_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    allowed = {
        "status",
        "lead_id",
        "upload_source",
        "reason",
        "recording_path",
        "payload_json",
    }
    parts: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        parts.append(f"{key} = ?")
        if key == "payload_json" and isinstance(value, dict):
            values.append(json.dumps(value))
        else:
            values.append(value)
    if not parts:
        return get_job(job_id)
    parts.append("updated_at = ?")
    values.append(_now_iso())
    values.append(job_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE kommo_call_jobs SET {', '.join(parts)} WHERE id = ?",
            values,
        )
        conn.commit()
    return get_job(job_id)


def list_jobs_by_status(statuses: list[str], limit: int = 50) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM kommo_call_jobs
            WHERE status IN ({placeholders})
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (*statuses, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_next_job(statuses: list[str]) -> Optional[dict[str, Any]]:
    """Atomically pick one job and mark it processing (avoids duplicate Kommo notes)."""
    placeholders = ",".join("?" for _ in statuses)
    now = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT id FROM kommo_call_jobs
            WHERE status IN ({placeholders})
            ORDER BY created_at ASC
            LIMIT 1
            """,
            tuple(statuses),
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            f"""
            UPDATE kommo_call_jobs
            SET status = 'processing', updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (now, row["id"], *statuses),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM kommo_call_jobs WHERE id = ?", (row["id"],)
        ).fetchone()
    return _row_to_dict(claimed) if claimed else None


def cleanup_old_jobs(days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM kommo_call_jobs WHERE created_at < ? AND status IN ('uploaded', 'failed', 'skipped')",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    return {
        "id": row["id"],
        "extension": row["extension"],
        "dedup_key": row["dedup_key"],
        "payload": payload,
        "status": row["status"],
        "lead_id": row["lead_id"],
        "upload_source": row["upload_source"],
        "reason": row["reason"],
        "recording_path": row["recording_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
