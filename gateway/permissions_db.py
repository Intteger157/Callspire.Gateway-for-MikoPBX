"""Local SQLite database for CallerID permissions, number labels and AMI configuration.

Stores which CallerID numbers each MikoPBX extension is allowed to use,
human-readable names/notes for trunk numbers, and the AMI connection
credentials used by the proxy to issue Originate commands.
"""

import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

_DB_PATH: str | None = None


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist.  Call once at startup."""
    global _DB_PATH
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "permissions.db")
    _DB_PATH = db_path

    conn = sqlite3.connect(_DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS callerid_permissions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            extension TEXT    NOT NULL,
            callerid  TEXT    NOT NULL,
            UNIQUE(extension, callerid)
        );

        CREATE TABLE IF NOT EXISTS ami_config (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            host     TEXT    DEFAULT '127.0.0.1',
            port     INTEGER DEFAULT 5038,
            username TEXT    DEFAULT '',
            secret   TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS callerid_names (
            number TEXT PRIMARY KEY,
            name   TEXT NOT NULL DEFAULT '',
            note   TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS trunk_manual_callerids (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            trunk_uniqid TEXT NOT NULL,
            callerid     TEXT NOT NULL,
            UNIQUE(trunk_uniqid, callerid)
        );

        CREATE TABLE IF NOT EXISTS app_users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            email                TEXT    NOT NULL UNIQUE,
            password_hash        TEXT    NOT NULL,
            mikopbx_extension    TEXT    NOT NULL,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            disabled             INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL,
            last_login_at        TEXT    NULL
        );

        INSERT OR IGNORE INTO ami_config (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS websoftphone_config (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            ws_url   TEXT NOT NULL DEFAULT '',
            sip_host TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO websoftphone_config (id) VALUES (1);
    """)
    conn.commit()
    conn.close()


def _conn() -> sqlite3.Connection:
    assert _DB_PATH, "permissions_db.init_db() must be called first"
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------- CallerID permissions ---------------

def get_allowed_callerids(extension: str) -> list[str]:
    conn = _conn()
    rows = conn.execute(
        "SELECT callerid FROM callerid_permissions WHERE extension = ? ORDER BY callerid",
        (extension,),
    ).fetchall()
    conn.close()
    return [r["callerid"] for r in rows]


def set_allowed_callerids(extension: str, callerids: list[str]) -> None:
    """Replace all CallerID permissions for *extension*."""
    conn = _conn()
    conn.execute("DELETE FROM callerid_permissions WHERE extension = ?", (extension,))
    for cid in callerids:
        conn.execute(
            "INSERT OR IGNORE INTO callerid_permissions (extension, callerid) VALUES (?, ?)",
            (extension, cid),
        )
    conn.commit()
    conn.close()


def get_all_permissions() -> list[dict]:
    """Return ``[{extension, callerids: [...]}, ...]`` for every extension that has at least one."""
    conn = _conn()
    rows = conn.execute(
        "SELECT extension, callerid FROM callerid_permissions ORDER BY extension, callerid"
    ).fetchall()
    conn.close()

    by_ext: dict[str, list[str]] = {}
    for r in rows:
        by_ext.setdefault(r["extension"], []).append(r["callerid"])
    return [{"extension": ext, "callerids": cids} for ext, cids in by_ext.items()]


# --------------- CallerID names / labels ---------------

def get_all_callerid_names() -> dict[str, dict]:
    """Return ``{number: {name, note}, ...}`` for all labelled numbers."""
    conn = _conn()
    rows = conn.execute("SELECT number, name, note FROM callerid_names ORDER BY number").fetchall()
    conn.close()
    return {r["number"]: {"name": r["name"], "note": r["note"]} for r in rows}


def set_callerid_name(number: str, name: str, note: str = "") -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO callerid_names (number, name, note) VALUES (?, ?, ?) "
        "ON CONFLICT(number) DO UPDATE SET name=excluded.name, note=excluded.note",
        (number, name, note),
    )
    conn.commit()
    conn.close()


def delete_callerid_name(number: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM callerid_names WHERE number = ?", (number,))
    conn.commit()
    conn.close()


# --------------- Manual CallerIDs per SIP trunk (contract DIDs) ---------------

def add_trunk_manual_callerid(trunk_uniqid: str, callerid: str) -> None:
    trunk_uniqid = (trunk_uniqid or "").strip()
    callerid = (callerid or "").strip()
    if not trunk_uniqid or not callerid:
        raise ValueError("trunk_uniqid and callerid are required")
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO trunk_manual_callerids (trunk_uniqid, callerid) VALUES (?, ?)",
        (trunk_uniqid, callerid),
    )
    conn.commit()
    conn.close()


def remove_trunk_manual_callerid(trunk_uniqid: str, callerid: str) -> None:
    conn = _conn()
    conn.execute(
        "DELETE FROM trunk_manual_callerids WHERE trunk_uniqid = ? AND callerid = ?",
        (trunk_uniqid, callerid),
    )
    conn.commit()
    conn.close()


def get_manual_callerids_by_trunk() -> dict[str, list[str]]:
    """``{trunk_uniqid: [callerid, ...], ...}`` sorted."""
    conn = _conn()
    rows = conn.execute(
        "SELECT trunk_uniqid, callerid FROM trunk_manual_callerids ORDER BY trunk_uniqid, callerid"
    ).fetchall()
    conn.close()
    by_trunk: dict[str, list[str]] = {}
    for r in rows:
        by_trunk.setdefault(r["trunk_uniqid"], []).append(r["callerid"])
    return by_trunk


def get_all_manual_trunk_callerid_numbers() -> list[str]:
    """Distinct manual numbers (for merging into global CallerID picker)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT DISTINCT callerid FROM trunk_manual_callerids ORDER BY callerid"
    ).fetchall()
    conn.close()
    return [r["callerid"] for r in rows]


# --------------- AMI configuration ---------------

def get_ami_config() -> dict:
    conn = _conn()
    row = conn.execute("SELECT host, port, username, secret FROM ami_config WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return {"host": "127.0.0.1", "port": 5038, "username": "", "secret": ""}
    return dict(row)


def set_ami_config(host: str, port: int, username: str, secret: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE ami_config SET host = ?, port = ?, username = ?, secret = ? WHERE id = 1",
        (host, port, username, secret),
    )
    conn.commit()
    conn.close()


# --------------- Web softphone WebRTC (public URLs, no secrets) ---------------

def get_webrtc_public_config() -> dict:
    conn = _conn()
    row = conn.execute("SELECT ws_url, sip_host FROM websoftphone_config WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return {"ws_url": "", "sip_host": ""}
    return {"ws_url": row["ws_url"] or "", "sip_host": row["sip_host"] or ""}


def set_webrtc_public_config(ws_url: str, sip_host: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE websoftphone_config SET ws_url = ?, sip_host = ? WHERE id = 1",
        (ws_url or "", sip_host or ""),
    )
    conn.commit()
    conn.close()


# --------------- App users (email login) ---------------

def create_app_user(email: str, password_hash: str, mikopbx_extension: str, must_change_password: bool = True) -> dict:
    email = (email or "").strip().lower()
    mikopbx_extension = (mikopbx_extension or "").strip()
    if not email or not mikopbx_extension:
        raise ValueError("email and mikopbx_extension are required")
    if "@" not in email:
        raise ValueError("email must look like an email address")
    if not password_hash:
        raise ValueError("password_hash is required")

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO app_users (email, password_hash, mikopbx_extension, must_change_password, disabled, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (email, password_hash, mikopbx_extension, 1 if must_change_password else 0, _now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, email, mikopbx_extension, must_change_password, disabled, created_at, last_login_at "
            "FROM app_users WHERE email = ?",
            (email,),
        ).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def get_app_user_by_email(email: str) -> dict | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, email, password_hash, mikopbx_extension, must_change_password, disabled, created_at, last_login_at "
            "FROM app_users WHERE email = ?",
            (email,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_app_users() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, email, mikopbx_extension, must_change_password, disabled, created_at, last_login_at "
            "FROM app_users ORDER BY email"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_app_user_password(email: str, password_hash: str, must_change_password: bool) -> None:
    email = (email or "").strip().lower()
    if not email or not password_hash:
        raise ValueError("email and password_hash are required")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET password_hash = ?, must_change_password = ? WHERE email = ?",
            (password_hash, 1 if must_change_password else 0, email),
        )
        conn.commit()
    finally:
        conn.close()


def set_app_user_must_change_password(email: str, must_change_password: bool) -> None:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET must_change_password = ? WHERE email = ?",
            (1 if must_change_password else 0, email),
        )
        conn.commit()
    finally:
        conn.close()


def set_app_user_disabled(email: str, disabled: bool) -> None:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET disabled = ? WHERE email = ?",
            (1 if disabled else 0, email),
        )
        conn.commit()
    finally:
        conn.close()


def mark_app_user_login(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET last_login_at = ? WHERE email = ?",
            (_now_iso(), email),
        )
        conn.commit()
    finally:
        conn.close()
