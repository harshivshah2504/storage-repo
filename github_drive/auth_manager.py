import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

from .api import GitHubClient, parse_owner_repo

DEFAULT_STATE_DIR = Path.home() / ".github-drive"
RENDER_DISK_STATE_DIR = Path("/var/data/github-drive")


def _resolve_state_dir() -> Path:
    configured = (os.environ.get("GITHUB_DRIVE_STATE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if Path("/var/data").is_dir():
        return RENDER_DISK_STATE_DIR
    return DEFAULT_STATE_DIR


APP_STATE_DIR = _resolve_state_dir()
TOKEN_FILE = APP_STATE_DIR / "token.json"
_MIGRATED_STATE = False


def ensure_state_dir() -> None:
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_state_if_needed()


def state_status() -> Dict:
    ensure_state_dir()
    return {
        "state_dir": str(APP_STATE_DIR),
        "token_file": str(TOKEN_FILE),
        "using_render_disk": str(APP_STATE_DIR).startswith(str(RENDER_DISK_STATE_DIR)),
        "state_dir_exists": APP_STATE_DIR.exists(),
        "state_dir_writable": os.access(APP_STATE_DIR, os.W_OK),
    }


def _migrate_legacy_state_if_needed() -> None:
    global _MIGRATED_STATE
    if _MIGRATED_STATE:
        return
    _MIGRATED_STATE = True
    if APP_STATE_DIR == DEFAULT_STATE_DIR or not DEFAULT_STATE_DIR.exists():
        return
    for filename in ("users.json", "token.json"):
        source = DEFAULT_STATE_DIR / filename
        destination = APP_STATE_DIR / filename
        if not source.exists() or destination.exists():
            continue
        try:
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
        except OSError:
            pass


def get_token() -> Optional[str]:
    """Return the GitHub PAT from env or the on-disk token file, or None."""
    env_value = os.environ.get("GITHUB_DRIVE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env_value:
        return env_value.strip()
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return (data.get("token") or "").strip() or None
        except (OSError, json.JSONDecodeError):
            return None
    return None


def save_token(token: str, owner: str = "", repo: str = "") -> Path:
    ensure_state_dir()
    payload = {"token": token.strip()}
    if owner:
        payload["owner"] = owner
    if repo:
        payload["repo"] = repo
    TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return TOKEN_FILE


def clear_token() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def get_repo_slug() -> Optional[str]:
    """Return the configured 'owner/repo' from env or the on-disk token file."""
    env_value = os.environ.get("GITHUB_DRIVE_REPO")
    if env_value:
        return env_value.strip()
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        owner = (data.get("owner") or "").strip()
        repo = (data.get("repo") or "").strip()
        if owner and repo:
            return f"{owner}/{repo}"
    return None


def resolve_owner_repo() -> Tuple[str, str]:
    slug = get_repo_slug()
    if not slug:
        raise RuntimeError(
            "GitHub repository is not configured. Set GITHUB_DRIVE_REPO=owner/repo "
            "or run 'github-drive auth --repo owner/repo'."
        )
    return parse_owner_repo(slug)


def get_client() -> GitHubClient:
    token = get_token()
    if not token:
        raise RuntimeError(
            "No GitHub token found. Set GITHUB_DRIVE_TOKEN or run 'github-drive auth --token <PAT>'."
        )
    owner, repo = resolve_owner_repo()
    return GitHubClient(token=token, owner=owner, repo=repo)


def auth_status() -> Dict:
    token = get_token()
    repo_slug = get_repo_slug()
    return {
        "token_present": bool(token),
        "token_source": _token_source(),
        "token_file": str(TOKEN_FILE),
        "repo": repo_slug or "",
        "repo_source": _repo_source(),
    }


def _token_source() -> str:
    if os.environ.get("GITHUB_DRIVE_TOKEN"):
        return "env:GITHUB_DRIVE_TOKEN"
    if os.environ.get("GITHUB_TOKEN"):
        return "env:GITHUB_TOKEN"
    if TOKEN_FILE.exists():
        return f"file:{TOKEN_FILE}"
    return "none"


def _repo_source() -> str:
    if os.environ.get("GITHUB_DRIVE_REPO"):
        return "env:GITHUB_DRIVE_REPO"
    if TOKEN_FILE.exists():
        return f"file:{TOKEN_FILE}"
    return "none"


def restore_from_env() -> None:
    """Legacy opt-in: mirror env token/repo into the on-disk CLI token file."""
    if (os.environ.get("GITHUB_DRIVE_MIRROR_ENV_TOKEN") or "").strip().lower() not in {"1", "true", "yes"}:
        return
    token = (os.environ.get("GITHUB_DRIVE_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return
    repo_slug = (os.environ.get("GITHUB_DRIVE_REPO") or "").strip()
    owner, repo = ("", "")
    if "/" in repo_slug:
        owner, repo = parse_owner_repo(repo_slug)
    save_token(token, owner=owner, repo=repo)


VALID_KEY_LENGTHS = (16, 24, 32)


def get_encryption_key() -> bytes:
    """Resolve the AES key used for client-side encryption.

    Priority order:
      1. GITHUB_DRIVE_ENCRYPTION_KEY  — hex- or base64-encoded raw key bytes (16/24/32 bytes).
         This is the recommended setting: stable across redeploys.
      2. GITHUB_DRIVE_SESSION_SECRET  — legacy fallback. The key is derived via
         HMAC-SHA256(session_secret, user_id). Only used when no encryption key is set.
         Rotates with the session secret, which is why you should migrate to (1).

    The CLI's `--key <passphrase>` overrides both at the call site.
    """
    explicit = (os.environ.get("GITHUB_DRIVE_ENCRYPTION_KEY") or "").strip()
    if explicit:
        return _decode_encryption_key(explicit)

    server_secret = os.environ.get("GITHUB_DRIVE_SESSION_SECRET")
    if server_secret:
        return derive_encode_key(_legacy_user_id())

    raise RuntimeError(
        "Encryption requires GITHUB_DRIVE_ENCRYPTION_KEY (preferred) or GITHUB_DRIVE_SESSION_SECRET. "
        "Generate a stable key with: python -m github_drive gen-key"
    )


def derive_encode_key(user_id: str) -> bytes:
    """Legacy: derive a 16-byte AES key from the session secret + user_id."""
    server_secret = os.environ.get("GITHUB_DRIVE_SESSION_SECRET")
    if not server_secret:
        raise RuntimeError("GITHUB_DRIVE_SESSION_SECRET environment variable must be set.")
    digest = hmac.new(
        server_secret.encode("utf-8"),
        user_id.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return digest[:16]


def generate_encryption_key(num_bytes: int = 32) -> str:
    """Return a fresh hex-encoded encryption key suitable for GITHUB_DRIVE_ENCRYPTION_KEY."""
    if num_bytes not in VALID_KEY_LENGTHS:
        raise ValueError(f"key length must be one of {VALID_KEY_LENGTHS}, got {num_bytes}")
    return secrets.token_hex(num_bytes)


def _legacy_user_id() -> str:
    return (os.environ.get("GITHUB_DRIVE_USER_ID") or "default").strip() or "default"


def _decode_encryption_key(value: str) -> bytes:
    """Accept hex or base64 (standard or url-safe). Reject anything that doesn't yield 16/24/32 bytes."""
    candidate = _try_hex(value) or _try_base64(value)
    if candidate is None:
        raise RuntimeError(
            "GITHUB_DRIVE_ENCRYPTION_KEY must be hex- or base64-encoded and decode to 16, 24, or 32 bytes."
        )
    return candidate


def _try_hex(value: str) -> Optional[bytes]:
    try:
        decoded = bytes.fromhex(value)
    except (ValueError, binascii.Error):
        return None
    return decoded if len(decoded) in VALID_KEY_LENGTHS else None


def _try_base64(value: str) -> Optional[bytes]:
    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded)
        except (ValueError, binascii.Error):
            continue
        if len(decoded) in VALID_KEY_LENGTHS:
            return decoded
    return None
