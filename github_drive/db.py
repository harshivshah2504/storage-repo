"""Postgres-backed account store.

Active when ``GITHUB_DRIVE_DATABASE_URL`` (preferred) or ``DATABASE_URL`` is set.
The schema is created on first use; there is no separate migration tool.

Tables
------

users (
  username       TEXT PRIMARY KEY,
  salt           TEXT NOT NULL,        -- hex, 16 bytes
  password_hash  TEXT NOT NULL,        -- hex, KDF output
  password_kdf   TEXT NOT NULL DEFAULT 'scrypt',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)

github_credentials (
  username        TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
  token_encrypted TEXT NOT NULL,       -- base64 of nonce|tag|ciphertext
  owner           TEXT NOT NULL,
  repo            TEXT NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional

LOG = logging.getLogger("github_drive.db")

_LOCK = threading.Lock()
_POOL = None
_INITIALIZED = False


def database_url() -> Optional[str]:
    raw = (
        os.environ.get("GITHUB_DRIVE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    return raw or None


def is_enabled() -> bool:
    return database_url() is not None


def _normalize_url(url: str) -> str:
    # Render and Heroku style URLs use the "postgres://" scheme; psycopg prefers postgresql://.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _open_pool():
    """Create and open a fresh connection pool. Caller holds _LOCK."""
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError(
            "psycopg_pool is required for Postgres mode. "
            "Install with: pip install 'psycopg[binary,pool]'"
        ) from exc

    url = database_url()
    if not url:
        raise RuntimeError("No DATABASE_URL configured.")

    # Connection-level kwargs.
    #
    # `prepare_threshold=None` disables psycopg 3's automatic prepared statements.
    # This is necessary when the connection goes through a transaction-pooler such as
    # Neon's `-pooler` endpoint or PgBouncer in transaction mode, where prepared
    # statements survive across pooled connections and cause "prepared statement already
    # exists" or first-query hangs. It is harmless on direct Postgres connections.
    #
    # `connect_timeout` makes a misconfigured URL fail fast (10 s) instead of hanging
    # the request until the gunicorn worker timeout.
    connect_kwargs = {
        "autocommit": False,
        "prepare_threshold": None,
        "connect_timeout": 10,
    }

    pool = ConnectionPool(
        _normalize_url(url),
        min_size=int(os.environ.get("GITHUB_DRIVE_DB_MIN_CONNECTIONS", "1")),
        max_size=int(os.environ.get("GITHUB_DRIVE_DB_MAX_CONNECTIONS", "10")),
        kwargs=connect_kwargs,
        timeout=15,                    # max wait when handing out a pooled connection
        open=False,
    )
    pool.open(wait=True, timeout=15.0)
    return pool


def get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _LOCK:
        if _POOL is not None:
            return _POOL
        _POOL = _open_pool()
        return _POOL


def close_pool() -> None:
    """Close the pool. Used by tests so each test starts fresh."""
    global _POOL, _INITIALIZED
    with _LOCK:
        if _POOL is not None:
            try:
                _POOL.close()
            except Exception:
                pass
        _POOL = None
        _INITIALIZED = False


@contextmanager
def connection():
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def ensure_schema() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                      username       TEXT PRIMARY KEY,
                      salt           TEXT NOT NULL,
                      password_hash  TEXT NOT NULL,
                      password_kdf   TEXT NOT NULL DEFAULT 'scrypt',
                      created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS github_credentials (
                      username        TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
                      token_encrypted TEXT NOT NULL,
                      owner           TEXT NOT NULL,
                      repo            TEXT NOT NULL,
                      updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            conn.commit()
        _INITIALIZED = True


# ── record-shape helpers ──────────────────────────────────────────────────────


def _row_to_record(user_row, creds_row) -> Optional[Dict]:
    if not user_row:
        return None
    record = {
        "username": user_row[0],
        "salt": user_row[1],
        "password_hash": user_row[2],
        "password_kdf": user_row[3] or "scrypt",
        "created_at": user_row[4].isoformat() if user_row[4] else "",
    }
    if creds_row and creds_row[0]:
        record["github"] = {
            "token_encrypted": creds_row[0],
            "owner": creds_row[1] or "",
            "repo": creds_row[2] or "",
            "updated_at": creds_row[3].isoformat() if creds_row[3] else "",
        }
    return record


# ── CRUD ──────────────────────────────────────────────────────────────────────


def get_user_record(username: str) -> Optional[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.username, u.salt, u.password_hash, u.password_kdf, u.created_at,
                       c.token_encrypted, c.owner, c.repo, c.updated_at
                FROM users u
                LEFT JOIN github_credentials c ON c.username = u.username
                WHERE u.username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_record(row[:5], row[5:])


def list_user_records() -> List[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.username, u.salt, u.password_hash, u.password_kdf, u.created_at,
                       c.token_encrypted, c.owner, c.repo, c.updated_at
                FROM users u
                LEFT JOIN github_credentials c ON c.username = u.username
                ORDER BY u.username
                """
            )
            rows = cur.fetchall()
    out: List[Dict] = []
    for row in rows:
        rec = _row_to_record(row[:5], row[5:])
        if rec:
            out.append(rec)
    return out


def insert_user_record(record: Dict) -> None:
    """Insert a fresh user row. Raises ValueError on duplicate username."""
    ensure_schema()
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for Postgres mode.") from exc
    with connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, salt, password_hash, password_kdf, created_at)
                    VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()))
                    """,
                    (
                        record["username"],
                        record["salt"],
                        record["password_hash"],
                        record.get("password_kdf") or "scrypt",
                        record.get("created_at") or None,
                    ),
                )
            conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise ValueError(f"User {record['username']!r} already exists.") from exc


def update_password(username: str, salt_hex: str, password_hash_hex: str, password_kdf: str) -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET salt = %s, password_hash = %s, password_kdf = %s, updated_at = NOW()
                WHERE username = %s
                """,
                (salt_hex, password_hash_hex, password_kdf, username),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Unknown user {username!r}.")
        conn.commit()


def delete_user(username: str) -> bool:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def upsert_credentials(username: str, token_encrypted: str, owner: str, repo: str) -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_credentials (username, token_encrypted, owner, repo)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET token_encrypted = EXCLUDED.token_encrypted,
                    owner = EXCLUDED.owner,
                    repo = EXCLUDED.repo,
                    updated_at = NOW()
                """,
                (username, token_encrypted, owner, repo),
            )
        conn.commit()


def clear_credentials(username: str) -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM github_credentials WHERE username = %s",
                (username,),
            )
        conn.commit()


def health() -> Dict:
    """Return basic connectivity info for /healthz."""
    if not is_enabled():
        return {"enabled": False}
    try:
        ensure_schema()
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"enabled": True, "ok": True}
    except Exception as exc:
        LOG.warning("DB health check failed: %s", exc)
        return {"enabled": True, "ok": False, "error": str(exc)}
