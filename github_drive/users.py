"""Per-user account store for the multi-tenant web frontend.

The CLI remains single-tenant (it reads ~/.github-drive/token.json directly). This module
backs the web app: signup, login, and per-user GitHub credentials.

Security notes:
  * Passwords are hashed with scrypt (stdlib) and a per-user random 16-byte salt.
  * GitHub PATs are encrypted at rest with AES-128-GCM using a key derived from
    GITHUB_DRIVE_SESSION_SECRET and the username. Rotating the session secret therefore
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

_LOCK = threading.Lock()

# scrypt parameters: ~30 ms on modern hardware, well above 100 ms is fine for login UX.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
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


def create_user(username: str, password: str) -> Dict:
    norm = validate_username(username)
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = secrets.token_bytes(SALT_BYTES)
    pw_hash = _scrypt(password, salt)
    record = {
        "username": norm,
        "salt": salt.hex(),
        "password_hash": pw_hash.hex(),
        "created_at": now_utc_iso(),
    }
    with _LOCK:
        users = _load_all()
        if norm in users:
            raise ValueError(f"User {norm!r} already exists.")
        users[norm] = record
        _save_all(users)
    return _public_view(record)


def verify_password(username: str, password: str) -> Optional[Dict]:
    norm = normalize_username(username)
    if not norm or not password:
        return None
    with _LOCK:
        users = _load_all()
    record = users.get(norm)
    if not record:
        return None
    salt = bytes.fromhex(record["salt"])
    expected = bytes.fromhex(record["password_hash"])
    actual = _scrypt(password, salt)
    if not hmac.compare_digest(actual, expected):
        return None
    return _public_view(record)


def change_password(username: str, new_password: str) -> None:
    norm = normalize_username(username)
    if not new_password or len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = secrets.token_bytes(SALT_BYTES)
    pw_hash = _scrypt(new_password, salt)
    with _LOCK:
        users = _load_all()
        record = users.get(norm)
        if not record:
            raise ValueError(f"Unknown user {norm!r}.")
        record["salt"] = salt.hex()
        record["password_hash"] = pw_hash.hex()
        # Existing PAT was encrypted under a key tied to the username, not the password,
        # so it stays valid across password changes.
        _save_all(users)


def list_users() -> List[Dict]:
    with _LOCK:
        users = _load_all()
    return [_public_view(u) for u in sorted(users.values(), key=lambda u: u["username"])]


def delete_user(username: str) -> bool:
    norm = normalize_username(username)
    with _LOCK:
        users = _load_all()
        if norm not in users:
            return False
        del users[norm]
        _save_all(users)
    return True


def set_user_credentials(username: str, token: str, repo_slug: str) -> Dict:
    """Validate `repo_slug` (owner/repo), encrypt the PAT, persist."""
    norm = normalize_username(username)
    if not token:
        raise ValueError("token is required")
    owner, repo = parse_owner_repo(repo_slug)
    encrypted = _encrypt_at_rest(token, norm)
    with _LOCK:
        users = _load_all()
        record = users.get(norm)
        if not record:
            raise ValueError(f"Unknown user {norm!r}.")
        record["github"] = {
            "token_encrypted": encrypted,
            "owner": owner,
            "repo": repo,
            "updated_at": now_utc_iso(),
        }
        _save_all(users)
    return {"owner": owner, "repo": repo}


def get_user_credentials(username: str) -> Optional[Dict]:
    """Return {token, owner, repo} for the user, or None if no PAT is configured."""
    norm = normalize_username(username)
    with _LOCK:
        users = _load_all()
    record = users.get(norm)
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
    with _LOCK:
        users = _load_all()
        record = users.get(norm)
        if not record:
            return
        record.pop("github", None)
        _save_all(users)


def get_user_status(username: str) -> Dict:
    norm = normalize_username(username)
    with _LOCK:
        users = _load_all()
    record = users.get(norm)
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


# ── internals ─────────────────────────────────────────────────────────────────


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


def _load_all() -> Dict[str, Dict]:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_all(users: Dict[str, Dict]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = USERS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(users, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass
