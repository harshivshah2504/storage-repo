"""Per-user account store for the multi-tenant web frontend.

Storage backend is selected at runtime:

  * **Postgres** — when ``GITHUB_DRIVE_DATABASE_URL`` (or ``DATABASE_URL``) is set.
    Recommended for hosted deployments; survives restarts and ephemeral disks.
  * **JSON file** — fallback for local dev. Path comes from ``auth_manager.APP_STATE_DIR``.

The public API (``create_user``, ``verify_password``, ``set_user_credentials`` etc.) does
not change; the backend only handles raw record persistence. All hashing, validation, and
at-rest encryption live in this module so the on-disk representation is identical between
backends.

Security notes
--------------
* Passwords are hashed with scrypt + a per-user random 16-byte salt (PBKDF2-SHA256 fallback
  if the runtime lacks ``hashlib.scrypt``).
* GitHub PATs are encrypted at rest with AES-128-GCM using a key derived from
  ``GITHUB_DRIVE_SESSION_SECRET`` and the username. Rotating the session secret
  invalidates stored PATs and forces every user to re-enter theirs on next login.
* Encryption keys for archive contents are mixed with the username so two users on
  the same server with the same per-archive plaintext produce different ciphertexts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .api import parse_owner_repo, now_utc_iso
from .auth_manager import APP_STATE_DIR

USERS_FILE = APP_STATE_DIR / "users.json"
USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,32}$")

# scrypt parameters: ~30 ms on modern hardware, well above 100 ms is fine for login UX.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PBKDF2_HASH = "sha256"
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


# ── public API ────────────────────────────────────────────────────────────────


def signup_enabled() -> bool:
    """Self-service signup is always available."""
    return True


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_username(username: str) -> str:
    norm = normalize_username(username)
    if not USERNAME_RE.match(norm):
        raise ValueError(
            "Username must be 2-32 chars, lowercase letters/digits/dot/underscore/hyphen only."
        )
    return norm


def backend_name() -> str:
    """Return ``'postgres'`` or ``'json'`` depending on which backend is currently active."""
    return _backend().name


def create_user(username: str, password: str) -> Dict:
    norm = validate_username(username)
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = secrets.token_bytes(SALT_BYTES)
    password_kdf = _preferred_password_kdf()
    pw_hash = _hash_password(password, salt, password_kdf)
    record = {
        "username": norm,
        "salt": salt.hex(),
        "password_hash": pw_hash.hex(),
        "password_kdf": password_kdf,
        "created_at": now_utc_iso(),
    }
    _backend().insert_user(record)
    return _public_view(record)


def verify_password(username: str, password: str) -> Optional[Dict]:
    norm = normalize_username(username)
    if not norm or not password:
        return None
    record = _backend().get_record(norm)
    if not record:
        return None
    salt = bytes.fromhex(record["salt"])
    expected = bytes.fromhex(record["password_hash"])
    password_kdf = (record.get("password_kdf") or "scrypt").strip().lower()
    actual = _hash_password(password, salt, password_kdf)
    if not hmac.compare_digest(actual, expected):
        return None
    return _public_view(record)


def change_password(username: str, new_password: str) -> None:
    norm = normalize_username(username)
    if not new_password or len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = secrets.token_bytes(SALT_BYTES)
    password_kdf = _preferred_password_kdf()
    pw_hash = _hash_password(new_password, salt, password_kdf)
    _backend().update_password(norm, salt.hex(), pw_hash.hex(), password_kdf)


def list_users() -> List[Dict]:
    return [_public_view(r) for r in _backend().list_records()]


def delete_user(username: str) -> bool:
    norm = normalize_username(username)
    return _backend().delete(norm)


def set_user_credentials(username: str, token: str, repo_slug: str) -> Dict:
    """Validate `repo_slug` (owner/repo), encrypt the PAT, persist."""
    norm = normalize_username(username)
    if not token:
        raise ValueError("token is required")
    owner, repo = parse_owner_repo(repo_slug)
    encrypted = _encrypt_at_rest(token, norm)
    _backend().set_credentials(norm, encrypted, owner, repo)
    return {"owner": owner, "repo": repo}


def get_user_credentials(username: str) -> Optional[Dict]:
    """Return {token, owner, repo} for the user, or None if no PAT is configured."""
    norm = normalize_username(username)
    record = _backend().get_record(norm)
    if not record:
        return None
    github = record.get("github") or {}
    encrypted = github.get("token_encrypted")
    if not encrypted:
        return None
    try:
        token = _decrypt_at_rest(encrypted, norm)
    except Exception as exc:
        raise RuntimeError(
            f"Could not decrypt stored PAT for {norm!r}: {exc}. "
            "If you rotated GITHUB_DRIVE_SESSION_SECRET, the user must re-enter their token."
        ) from exc
    return {"token": token, "owner": github.get("owner", ""), "repo": github.get("repo", "")}


def clear_user_credentials(username: str) -> None:
    norm = normalize_username(username)
    _backend().clear_credentials(norm)


def get_user_status(username: str) -> Dict:
    norm = normalize_username(username)
    record = _backend().get_record(norm)
    if not record:
        return {"username": norm, "exists": False}
    github = record.get("github") or {}
    return {
        "username": norm,
        "exists": True,
        "token_present": bool(github.get("token_encrypted")),
        "repo": f"{github.get('owner','')}/{github.get('repo','')}" if github.get("owner") and github.get("repo") else "",
        "created_at": record.get("created_at", ""),
    }


def migrate_json_to_db() -> Dict:
    """Copy every record from the JSON file into the configured Postgres database.

    Skips users that already exist in the database; never overwrites their password or PAT.
    Useful after switching a deployment from file-based storage to a managed database.
    """
    from . import db

    if not db.is_enabled():
        raise RuntimeError(
            "GITHUB_DRIVE_DATABASE_URL is not set; nothing to migrate to. "
            "Set it before running this command."
        )
    json_backend = _JSONBackend()
    pg_backend = _PostgresBackend()
    inserted = 0
    skipped = 0
    creds_copied = 0
    for record in json_backend.list_records():
        username = record["username"]
        if pg_backend.get_record(username) is not None:
            skipped += 1
            continue
        pg_backend.insert_user(record)
        inserted += 1
        github = record.get("github") or {}
        if github.get("token_encrypted") and github.get("owner") and github.get("repo"):
            pg_backend.set_credentials(
                username,
                github["token_encrypted"],
                github["owner"],
                github["repo"],
            )
            creds_copied += 1
    return {"inserted": inserted, "skipped": skipped, "credentials_copied": creds_copied}


# ── encryption helpers ────────────────────────────────────────────────────────


def derive_user_archive_key(username: str) -> bytes:
    """Per-user 16-byte AES key for archive contents.

    Derives from GITHUB_DRIVE_ENCRYPTION_KEY (preferred) or GITHUB_DRIVE_SESSION_SECRET,
    HMAC-mixed with the username so two users on the same server who happen to share
    a password and a plaintext do not produce identical ciphertexts.
    """
    from .auth_manager import get_encryption_key

    base_key = get_encryption_key()
    return hmac.new(base_key, f"archive:{normalize_username(username)}".encode("utf-8"), hashlib.sha256).digest()[:16]


# ── backend abstraction ──────────────────────────────────────────────────────


class _Backend:
    name = "abstract"

    def get_record(self, username: str) -> Optional[Dict]: raise NotImplementedError
    def list_records(self) -> List[Dict]: raise NotImplementedError
    def insert_user(self, record: Dict) -> None: raise NotImplementedError
    def update_password(self, username: str, salt_hex: str, password_hash_hex: str, password_kdf: str) -> None: raise NotImplementedError
    def delete(self, username: str) -> bool: raise NotImplementedError
    def set_credentials(self, username: str, token_encrypted: str, owner: str, repo: str) -> None: raise NotImplementedError
    def clear_credentials(self, username: str) -> None: raise NotImplementedError


class _JSONBackend(_Backend):
    name = "json"
    _LOCK = threading.Lock()

    def get_record(self, username: str) -> Optional[Dict]:
        with self._LOCK:
            users = self._load_all()
        return users.get(username)

    def list_records(self) -> List[Dict]:
        with self._LOCK:
            users = self._load_all()
        return sorted(users.values(), key=lambda u: u["username"])

    def insert_user(self, record: Dict) -> None:
        username = record["username"]
        with self._LOCK:
            users = self._load_all()
            if username in users:
                raise ValueError(f"User {username!r} already exists.")
            users[username] = record
            self._save_all(users)

    def update_password(self, username, salt_hex, password_hash_hex, password_kdf) -> None:
        with self._LOCK:
            users = self._load_all()
            record = users.get(username)
            if not record:
                raise ValueError(f"Unknown user {username!r}.")
            record["salt"] = salt_hex
            record["password_hash"] = password_hash_hex
            record["password_kdf"] = password_kdf
            self._save_all(users)

    def delete(self, username: str) -> bool:
        with self._LOCK:
            users = self._load_all()
            if username not in users:
                return False
            del users[username]
            self._save_all(users)
        return True

    def set_credentials(self, username, token_encrypted, owner, repo) -> None:
        with self._LOCK:
            users = self._load_all()
            record = users.get(username)
            if not record:
                raise ValueError(f"Unknown user {username!r}.")
            record["github"] = {
                "token_encrypted": token_encrypted,
                "owner": owner,
                "repo": repo,
                "updated_at": now_utc_iso(),
            }
            self._save_all(users)

    def clear_credentials(self, username: str) -> None:
        with self._LOCK:
            users = self._load_all()
            record = users.get(username)
            if not record:
                return
            record.pop("github", None)
            self._save_all(users)

    @staticmethod
    def _load_all() -> Dict[str, Dict]:
        if not USERS_FILE.exists():
            return {}
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _save_all(users: Dict[str, Dict]) -> None:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = USERS_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(users, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, USERS_FILE)
        try:
            os.chmod(USERS_FILE, 0o600)
        except OSError:
            pass


class _PostgresBackend(_Backend):
    name = "postgres"

    def get_record(self, username: str) -> Optional[Dict]:
        from . import db
        return db.get_user_record(username)

    def list_records(self) -> List[Dict]:
        from . import db
        return db.list_user_records()

    def insert_user(self, record: Dict) -> None:
        from . import db
        db.insert_user_record(record)

    def update_password(self, username, salt_hex, password_hash_hex, password_kdf) -> None:
        from . import db
        db.update_password(username, salt_hex, password_hash_hex, password_kdf)

    def delete(self, username: str) -> bool:
        from . import db
        return db.delete_user(username)

    def set_credentials(self, username, token_encrypted, owner, repo) -> None:
        from . import db
        # Ensure the user exists so we get a clean error rather than an opaque FK violation.
        if db.get_user_record(username) is None:
            raise ValueError(f"Unknown user {username!r}.")
        db.upsert_credentials(username, token_encrypted, owner, repo)

    def clear_credentials(self, username: str) -> None:
        from . import db
        db.clear_credentials(username)


def _backend() -> _Backend:
    from . import db
    if db.is_enabled():
        return _PostgresBackend()
    return _JSONBackend()


# ── shared internals ─────────────────────────────────────────────────────────


def _public_view(record: Dict) -> Dict:
    github = record.get("github") or {}
    return {
        "username": record["username"],
        "created_at": record.get("created_at", ""),
        "token_present": bool(github.get("token_encrypted")),
        "repo": f"{github['owner']}/{github['repo']}" if github.get("owner") and github.get("repo") else "",
    }


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=128 * SCRYPT_N * SCRYPT_R * 2,
    )


def _pbkdf2(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        PBKDF2_HASH,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=SCRYPT_DKLEN,
    )


def _preferred_password_kdf() -> str:
    return "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2_sha256"


def _hash_password(password: str, salt: bytes, password_kdf: str) -> bytes:
    kind = (password_kdf or "").strip().lower()
    if kind == "scrypt":
        if not hasattr(hashlib, "scrypt"):
            raise RuntimeError(
                "This Python runtime does not support hashlib.scrypt, so existing scrypt-hashed "
                "passwords cannot be verified here."
            )
        return _scrypt(password, salt)
    if kind == "pbkdf2_sha256":
        return _pbkdf2(password, salt)
    raise RuntimeError(f"Unsupported password_kdf {password_kdf!r}.")


def _at_rest_key(username: str) -> bytes:
    secret = (os.environ.get("GITHUB_DRIVE_SESSION_SECRET") or "").strip()
    if not secret:
        raise RuntimeError(
            "GITHUB_DRIVE_SESSION_SECRET must be set to encrypt/decrypt user PATs at rest."
        )
    return hmac.new(
        secret.encode("utf-8"),
        f"at-rest:{normalize_username(username)}".encode("utf-8"),
        hashlib.sha256,
    ).digest()[:16]


def _encrypt_at_rest(plaintext: str, username: str) -> str:
    from Crypto.Cipher import AES

    key = _at_rest_key(username)
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(nonce + tag + ct).decode("ascii")


def _decrypt_at_rest(encoded: str, username: str) -> str:
    from Crypto.Cipher import AES

    raw = base64.b64decode(encoded.encode("ascii"))
    nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
    key = _at_rest_key(username)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    pt = cipher.decrypt_and_verify(ct, tag)
    return pt.decode("utf-8")
