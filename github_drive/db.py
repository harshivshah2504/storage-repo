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

github_oauth_accounts (
  github_id    TEXT PRIMARY KEY,
  username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  github_login TEXT NOT NULL,
  email        TEXT,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)

github_repositories (
  username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  owner       TEXT NOT NULL,
  repo        TEXT NOT NULL,
  is_active   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (username, owner, repo)
)

web_tasks (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  type          TEXT NOT NULL,
  status        TEXT NOT NULL,
  created_at    DOUBLE PRECISION NOT NULL,
  updated_at    DOUBLE PRECISION NOT NULL,
  payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
  logs          JSONB NOT NULL DEFAULT '[]'::jsonb,
  result        JSONB,
  error         TEXT,
  progress_total INTEGER NOT NULL DEFAULT 0,
  progress_done  INTEGER NOT NULL DEFAULT 0,
  last_event    TEXT
)

abuse_reports (
  id          TEXT PRIMARY KEY,
  reporter    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  subject     TEXT NOT NULL,
  details     TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional

LOG = logging.getLogger("github_drive.db")

# Schema creation can open the pool while already holding the DB guard.
# Use an RLock so the same thread can safely enter get_pool() from ensure_schema().
_LOCK = threading.RLock()
_POOL = None
_INITIALIZED = False


class DatabaseUnavailableError(RuntimeError):
    """Raised when the configured Postgres backend cannot hand out a connection."""


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


def _uses_neon_pooler(url: str) -> bool:
    return "-pooler." in (url or "")


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
    # `autocommit=True` means each statement is its own transaction. SELECT-only paths
    # don't leave implicit transactions parked on the connection, which is what causes
    # PgBouncer transaction-pooling endpoints (e.g. Neon's `-pooler` host) to wedge.
    #
    # `prepare_threshold=None` disables psycopg 3's automatic prepared statements.
    # Required for transaction poolers because prepared statements survive across pooled
    # connections and cause "prepared statement already exists" or first-query hangs.
    #
    # `connect_timeout` makes a misconfigured URL fail fast (10 s) instead of hanging
    # the request until the gunicorn worker timeout.
    #
    # Avoid passing `statement_timeout` in startup options to Neon's pooled endpoints:
    # the pooler rejects it as an unsupported startup parameter.
    normalized_url = _normalize_url(url)
    connect_kwargs = {
        "autocommit": True,
        "prepare_threshold": None,
        "connect_timeout": 10,
    }
    if not _uses_neon_pooler(normalized_url):
        # On direct Postgres connections, keep a server-side statement timeout so stuck
        # queries cannot pin a worker forever.
        connect_kwargs["options"] = "-c statement_timeout=15000"

    max_connections = max(1, int(os.environ.get("GITHUB_DRIVE_DB_MAX_CONNECTIONS", "4")))
    min_connections = max(0, int(os.environ.get("GITHUB_DRIVE_DB_MIN_CONNECTIONS", "0")))
    min_connections = min(min_connections, max_connections)

    pool_timeout = max(1.0, float(os.environ.get("GITHUB_DRIVE_DB_POOL_TIMEOUT_SECONDS", "15")))
    max_lifetime = max(30.0, float(os.environ.get("GITHUB_DRIVE_DB_MAX_LIFETIME_SECONDS", "1800")))
    max_idle = max(5.0, float(os.environ.get("GITHUB_DRIVE_DB_MAX_IDLE_SECONDS", "300")))
    reconnect_timeout = max(5.0, float(os.environ.get("GITHUB_DRIVE_DB_RECONNECT_TIMEOUT_SECONDS", "30")))

    pool = ConnectionPool(
        normalized_url,
        min_size=min_connections,
        max_size=max_connections,
        kwargs=connect_kwargs,
        # Validate pooled connections on checkout so long-idle deployments don't hand out
        # dead sockets after the database/provider has suspended or rotated them away.
        check=ConnectionPool.check_connection,
        timeout=pool_timeout,
        max_lifetime=max_lifetime,
        max_idle=max_idle,
        reconnect_timeout=reconnect_timeout,
        open=False,
    )
    pool.open(wait=True, timeout=pool_timeout)
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
    for attempt in (1, 2):
        pool = get_pool()
        try:
            with pool.connection() as conn:
                yield conn
                return
        except Exception as exc:
            if type(exc).__name__ == "PoolTimeout":
                LOG.warning("Database connection pool timed out on attempt %s: %s", attempt, exc)
                if attempt == 1:
                    LOG.warning("Resetting database pool after timeout and retrying once.")
                    close_pool()
                    continue
                raise DatabaseUnavailableError(
                    "Database is temporarily unavailable. After long idle periods this can mean "
                    "the provider paused the database or old pooled connections expired. "
                    "Retry in a few seconds, increase GITHUB_DRIVE_DB_MAX_CONNECTIONS, or check "
                    "whether your Postgres provider has gone idle."
                ) from exc
            raise


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
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS github_oauth_accounts (
                      github_id    TEXT PRIMARY KEY,
                      username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                      github_login TEXT NOT NULL,
                      email        TEXT,
                      updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS github_repositories (
                      username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                      owner       TEXT NOT NULL,
                      repo        TEXT NOT NULL,
                      is_active   BOOLEAN NOT NULL DEFAULT FALSE,
                      created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      PRIMARY KEY (username, owner, repo)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_tasks (
                      id             TEXT PRIMARY KEY,
                      user_id        TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                      type           TEXT NOT NULL,
                      status         TEXT NOT NULL,
                      created_at     DOUBLE PRECISION NOT NULL,
                      updated_at     DOUBLE PRECISION NOT NULL,
                      payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
                      logs           JSONB NOT NULL DEFAULT '[]'::jsonb,
                      result         JSONB,
                      error          TEXT,
                      progress_total INTEGER NOT NULL DEFAULT 0,
                      progress_done  INTEGER NOT NULL DEFAULT 0,
                      last_event     TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS abuse_reports (
                      id          TEXT PRIMARY KEY,
                      reporter    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                      subject     TEXT NOT NULL,
                      details     TEXT NOT NULL,
                      created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO github_repositories (username, owner, repo, is_active)
                    SELECT username, owner, repo, TRUE
                    FROM github_credentials
                    WHERE owner <> '' AND repo <> ''
                    ON CONFLICT (username, owner, repo) DO UPDATE
                    SET is_active = TRUE,
                        updated_at = NOW()
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
            if owner and repo:
                cur.execute(
                    """
                    UPDATE github_repositories
                    SET is_active = FALSE
                    WHERE username = %s
                    """,
                    (username,),
                )
                cur.execute(
                    """
                    INSERT INTO github_repositories (username, owner, repo, is_active)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (username, owner, repo) DO UPDATE
                    SET is_active = TRUE,
                        updated_at = NOW()
                    """,
                    (username, owner, repo),
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
            cur.execute(
                "DELETE FROM github_repositories WHERE username = %s",
                (username,),
            )
        conn.commit()


def list_user_repositories(username: str) -> List[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT owner, repo, is_active, updated_at
                FROM github_repositories
                WHERE username = %s
                ORDER BY is_active DESC, updated_at DESC, owner ASC, repo ASC
                """,
                (username,),
            )
            rows = cur.fetchall()
    return [
        {
            "owner": row[0],
            "repo": row[1],
            "slug": f"{row[0]}/{row[1]}",
            "active": bool(row[2]),
            "updated_at": row[3].isoformat() if row[3] else "",
        }
        for row in rows
    ]


def get_oauth_account_for_username(username: str) -> Optional[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT github_id, username, github_login, email, updated_at
                FROM github_oauth_accounts
                WHERE username = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (username,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "github_id": row[0],
        "username": row[1],
        "github_login": row[2],
        "email": row[3] or "",
        "updated_at": row[4].isoformat() if row[4] else "",
    }


def get_oauth_account(github_id: str) -> Optional[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT github_id, username, github_login, email, updated_at
                FROM github_oauth_accounts
                WHERE github_id = %s
                """,
                (github_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "github_id": row[0],
        "username": row[1],
        "github_login": row[2],
        "email": row[3] or "",
        "updated_at": row[4].isoformat() if row[4] else "",
    }


def upsert_oauth_account(github_id: str, username: str, github_login: str, email: str = "") -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_oauth_accounts (github_id, username, github_login, email)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (github_id) DO UPDATE
                SET username = EXCLUDED.username,
                    github_login = EXCLUDED.github_login,
                    email = EXCLUDED.email,
                    updated_at = NOW()
                """,
                (github_id, username, github_login, email),
            )
        conn.commit()


def insert_task_record(task: Dict) -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO web_tasks (
                  id, user_id, type, status, created_at, updated_at, payload, logs,
                  result, error, progress_total, progress_done, last_event
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    task["id"],
                    task["user_id"],
                    task["type"],
                    task["status"],
                    float(task["created_at"]),
                    float(task["updated_at"]),
                    _json(task.get("payload") or {}),
                    _json(task.get("logs") or []),
                    _json(task.get("result")) if task.get("result") is not None else None,
                    task.get("error"),
                    int(task.get("progress_total") or 0),
                    int(task.get("progress_done") or 0),
                    task.get("last_event"),
                ),
            )
        conn.commit()


def update_task_record(task_id: str, updates: Dict) -> None:
    if not updates:
        return
    ensure_schema()
    allowed = {
        "status", "updated_at", "payload", "logs", "result", "error",
        "progress_total", "progress_done", "last_event",
    }
    assignments = []
    values = []
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key in {"payload", "logs", "result"}:
            assignments.append(f"{key} = %s::jsonb")
            values.append(_json(value) if value is not None else None)
        elif key in {"progress_total", "progress_done"}:
            assignments.append(f"{key} = %s")
            values.append(int(value or 0))
        elif key == "updated_at":
            assignments.append(f"{key} = %s")
            values.append(float(value))
        else:
            assignments.append(f"{key} = %s")
            values.append(value)
    if not assignments:
        return
    values.append(task_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE web_tasks SET {', '.join(assignments)} WHERE id = %s",
                tuple(values),
            )
        conn.commit()


def get_task_record(task_id: str) -> Optional[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, type, status, created_at, updated_at, payload, logs,
                       result, error, progress_total, progress_done, last_event
                FROM web_tasks
                WHERE id = %s
                """,
                (task_id,),
            )
            row = cur.fetchone()
    return _task_row_to_record(row)


def list_task_records(user_id: str, limit: int = 50) -> List[Dict]:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, type, status, created_at, updated_at, payload, logs,
                       result, error, progress_total, progress_done, last_event
                FROM web_tasks
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, int(limit)),
            )
            rows = cur.fetchall()
    return [record for record in (_task_row_to_record(row) for row in rows) if record]


def fail_stale_running_tasks() -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE web_tasks
                SET status = 'failed',
                    error = COALESCE(error, 'Server restarted before this task completed.'),
                    updated_at = EXTRACT(EPOCH FROM NOW())
                WHERE status IN ('queued', 'running')
                """
            )
        conn.commit()


def insert_abuse_report(report_id: str, reporter: str, subject: str, details: str) -> None:
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO abuse_reports (id, reporter, subject, details)
                VALUES (%s, %s, %s, %s)
                """,
                (report_id, reporter, subject, details),
            )
        conn.commit()


def health() -> Dict:
    """Return basic connectivity info for CLI/admin diagnostics."""
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


def _json(value) -> str:
    import json

    return json.dumps(value)


def _task_row_to_record(row) -> Optional[Dict]:
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "type": row[2],
        "status": row[3],
        "created_at": float(row[4] or 0),
        "updated_at": float(row[5] or 0),
        "payload": row[6] or {},
        "logs": row[7] or [],
        "result": row[8],
        "error": row[9],
        "progress_total": int(row[10] or 0),
        "progress_done": int(row[11] or 0),
        "last_event": row[12],
    }
