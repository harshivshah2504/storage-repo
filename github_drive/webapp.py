import base64
import functools
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import warnings
import zipfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlencode

from flask import (
    Flask,
    Response,
    abort,
    after_this_request,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import moderation, task_store, users
from .api import GitHubClient, parse_owner_repo
from .auth_manager import ensure_state_dir, restore_from_env, state_status
from .limits import RateLimitExceeded, check_rate_limit, env_int
from .storage import (
    append_to_archive,
    create_empty_archive,
    create_archive_folder,
    delete_archive,
    delete_archive_file,
    download_archive,
    fetch_archive_file_to_disk,
    list_archive_contents,
    list_remote_archives,
    list_remote_archives_page,
    upload_browser_single_file,
    upload_archive,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB
DEFAULT_USER_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB per browser upload by default.
LOG = logging.getLogger("github_drive.webapp")

_TASK_LOCK = threading.Lock()
_TASK_CACHE: Dict[str, Dict[str, Any]] = {}
_TASK_CACHE_META: Dict[str, Dict[str, Any]] = {}
_TASK_PROGRESS_FLUSH_SECONDS = max(
    0.25,
    float(os.environ.get("GITHUB_DRIVE_TASK_PROGRESS_FLUSH_SECONDS", "1.5")),
)
_TASK_PROGRESS_FLUSH_ITEMS = max(
    1,
    int(os.environ.get("GITHUB_DRIVE_TASK_PROGRESS_FLUSH_ITEMS", "25")),
)
_DOWNLOAD_DIRS: Dict[str, str] = {}
_DOWNLOAD_LOCK = threading.Lock()
_TASK_QUEUE: List[Dict[str, Any]] = []
_TASK_QUEUE_COND = threading.Condition()
_TASK_ACTIVE_RUNNERS = 0
_TASK_ACTIVE_IDS: Set[str] = set()
_TASK_DISPATCHER_STARTED = False
_TASK_ORPHAN_GRACE_SECONDS = max(
    10.0,
    float(os.environ.get("GITHUB_DRIVE_TASK_ORPHAN_GRACE_SECONDS", "30")),
)


def create_app() -> Flask:
    from . import db as db_module
    from werkzeug.middleware.proxy_fix import ProxyFix

    ensure_state_dir()
    restore_from_env()
    task_store.init_store()

    secret = os.environ.get("GITHUB_DRIVE_SESSION_SECRET")
    if not secret:
        secret = secrets.token_hex(32)
        LOG.warning(
            "GITHUB_DRIVE_SESSION_SECRET is not set; generated an ephemeral one. "
            "Set this env var in production so sessions, stored PATs, and the encryption "
            "key remain stable across restarts."
        )
        os.environ["GITHUB_DRIVE_SESSION_SECRET"] = secret

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = secret
    app.config["MAX_CONTENT_LENGTH"] = _max_upload_bytes()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _cookie_secure_default()
    app.permanent_session_lifetime = 60 * 60 * 24 * 14  # 14 days
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    _ensure_task_dispatcher()

    @app.context_processor
    def inject_security_context():
        return {"csrf_token": _csrf_token}

    @app.before_request
    def enforce_csrf():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        expected = _csrf_token()
        supplied = _csrf_from_request()
        if not supplied or not secrets.compare_digest(supplied, expected):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Invalid or missing CSRF token."}), 403
            abort(403)
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    @app.errorhandler(db_module.DatabaseUnavailableError)
    def handle_database_unavailable(exc):
        message = str(exc) or "Database is temporarily unavailable. Please try again."
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), 503
        if request.path in {"/login", "/signup"}:
            return _render_auth_page(
                mode="signup" if request.path == "/signup" else "login",
                error=message,
                status=503,
            )
        return Response(message, status=503, mimetype="text/plain")

    status = state_status()
    if os.environ.get("RENDER") and not status["using_render_disk"]:
        LOG.warning(
            "Render detected but account state is not using /var/data. "
            "Users will not persist across restarts. Current state_dir=%s",
            status["state_dir"],
        )

    basic_auth = _read_basic_auth_credentials()
    if basic_auth:
        app.before_request(_make_basic_auth_guard(basic_auth))

    # ── public routes (no login) ─────────────────────────────────────────────

    @app.get("/healthz")
    def healthz():
        """Pure liveness probe. Never exposes state paths, database details, or repo data."""
        return jsonify({"ok": True})

    @app.get("/warm-db")
    def warm_db():
        """Secret-protected endpoint for external cron jobs to wake the database."""
        from . import db as _db

        expected_token = (os.environ.get("GITHUB_DRIVE_DB_WARM_TOKEN") or "").strip()
        if not expected_token:
            abort(404)
        supplied_token = _warm_db_token()
        if not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
            return jsonify({"ok": False, "error": "Forbidden"}), 403
        if not _db.is_enabled():
            return jsonify({"ok": False, "error": "GITHUB_DRIVE_DATABASE_URL is not set."}), 503

        result_holder: Dict[str, Any] = {}
        done = threading.Event()

        def probe() -> None:
            from . import db as inner_db
            try:
                with inner_db.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                result_holder["result"] = {"ok": True, "warmed": True}
            except Exception as exc:
                result_holder["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                done.set()

        thread = threading.Thread(target=probe, daemon=True, name="warm-db")
        thread.start()

        if not done.wait(timeout=20.0):
            return jsonify({
                "ok": False,
                "error": "DB warm-up did not complete in 20 s. Provider may still be waking up.",
            }), 504

        result = result_holder.get("result", {"ok": False, "error": "no result captured"})
        return jsonify(result), (200 if result.get("ok") else 503)

    @app.get("/api/db-check")
    def api_db_check():
        """Hard-bounded DB connectivity diagnostic. Runs the connection attempt in a
        daemon thread; the request returns within 20 s no matter what libpq is doing.
        A wedged probe thread is left dangling and reaped at process exit — that's the
        whole point: we must never block the request handler on a stuck C call."""
        from . import db as _db

        if (os.environ.get("GITHUB_DRIVE_ENABLE_DB_CHECK") or "").strip().lower() not in {"1", "true", "yes"}:
            abort(404)
        if not session.get("user_id"):
            return jsonify({"error": "Login required"}), 401
        if not _db.is_enabled():
            return jsonify({"ok": False, "error": "GITHUB_DRIVE_DATABASE_URL is not set."}), 503

        result_holder: Dict[str, Any] = {}
        done = threading.Event()

        def probe() -> None:
            from . import db as inner_db
            try:
                with inner_db.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT version()")
                        version_row = cur.fetchone()
                        cur.execute(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_name IN ('users', 'github_credentials')"
                        )
                        tables_row = cur.fetchone()
                result_holder["result"] = {
                    "ok": True,
                    "server_version": version_row[0] if version_row else "?",
                    "schema_initialized": int(tables_row[0]) >= 2 if tables_row else False,
                }
            except Exception as exc:
                result_holder["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                done.set()

        thread = threading.Thread(target=probe, daemon=True, name="db-check")
        thread.start()

        if not done.wait(timeout=20.0):
            return jsonify({
                "ok": False,
                "error": "DB probe did not complete in 20 s. Worker thread left running. "
                         "Likely network/firewall block, wrong hostname, or libpq stuck in TLS "
                         "negotiation. Verify GITHUB_DRIVE_DATABASE_URL hostname is reachable.",
            }), 504

        result = result_holder.get("result", {"ok": False, "error": "no result captured"})
        return jsonify(result), (200 if result.get("ok") else 502)

    @app.get("/login")
    def login_page():
        return _render_auth_page(mode="login")

    @app.post("/login")
    def login_submit():
        try:
            _limit_auth_attempt("login")
        except RateLimitExceeded as exc:
            return _render_auth_page(mode="login", error=str(exc), status=429)
        if _turnstile_enabled():
            error = _verify_turnstile("auth")
            if error:
                return _render_auth_page(mode="login", error=error, status=400)
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = users.verify_password(username, password)
        if not user:
            return _render_auth_page(
                mode="login",
                error="Invalid username or password.",
                status=401,
            )
        session.clear()
        session["user_id"] = user["username"]
        session.permanent = True
        return redirect(url_for("index"))

    @app.get("/signup")
    def signup_page():
        if not users.signup_enabled():
            if _github_oauth_enabled() and _github_oauth_signup_enabled():
                return _render_auth_page(mode="signup")
            return _signup_disabled_response()
        return _render_auth_page(mode="signup")

    @app.post("/signup")
    def signup_submit():
        if not users.signup_enabled():
            return _signup_disabled_response()
        try:
            _limit_auth_attempt("signup")
        except RateLimitExceeded as exc:
            return _render_auth_page(mode="signup", error=str(exc), status=429)
        if _turnstile_enabled():
            error = _verify_turnstile("auth")
            if error:
                return _render_auth_page(mode="signup", error=error, status=400)
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if password != confirm:
            return _render_auth_page(
                mode="signup",
                error="Passwords do not match.",
                status=400,
            )
        try:
            user = users.create_user(username, password)
        except ValueError as exc:
            return _render_auth_page(mode="signup", error=str(exc), status=400)
        session.clear()
        session["user_id"] = user["username"]
        session.permanent = True
        return redirect(url_for("index"))

    @app.route("/auth/github", methods=["GET", "POST"])
    def github_oauth_start():
        if not _github_oauth_enabled():
            abort(404)
        if _turnstile_enabled() and request.method != "POST":
            return _render_auth_page(
                mode="signup" if request.args.get("mode") == "signup" else "login",
                error="Use the GitHub sign-in button to complete verification.",
                status=405,
            )
        try:
            _limit_auth_attempt("github-oauth")
        except RateLimitExceeded as exc:
            return _render_auth_page(mode="login", error=str(exc), status=429)
        if _turnstile_enabled():
            error = _verify_turnstile("auth")
            if error:
                mode = "signup" if (request.form.get("mode") or "").strip() == "signup" else "login"
                return _render_auth_page(mode=mode, error=error, status=400)
        state = secrets.token_urlsafe(32)
        session["github_oauth_state"] = state
        params = {
            "client_id": os.environ["GITHUB_OAUTH_CLIENT_ID"].strip(),
            "redirect_uri": _github_oauth_redirect_uri(),
            "scope": (os.environ.get("GITHUB_OAUTH_SCOPE") or "repo read:user user:email").strip(),
            "state": state,
            "allow_signup": "true" if _github_oauth_signup_enabled() else "false",
        }
        return redirect(f"https://github.com/login/oauth/authorize?{urlencode(params)}")

    @app.get("/auth/github/callback")
    def github_oauth_callback():
        if not _github_oauth_enabled():
            abort(404)
        supplied_state = (request.args.get("state") or "").strip()
        expected_state = session.pop("github_oauth_state", "")
        if not supplied_state or not expected_state or not secrets.compare_digest(supplied_state, expected_state):
            return _oauth_error("GitHub sign-in could not be verified. Please try again.")
        code = (request.args.get("code") or "").strip()
        if not code:
            return _oauth_error("GitHub did not return an authorization code.")
        try:
            token = _github_oauth_exchange_code(code)
            profile = _github_oauth_profile(token)
            if not _github_oauth_signup_enabled() and not users.get_oauth_account(str(profile["id"])):
                raise RuntimeError("Signup is disabled on this server. Contact the administrator to create an account.")
            user = users.create_oauth_user(
                github_id=str(profile["id"]),
                github_login=profile["login"],
                email=profile.get("email") or "",
            )
            users.set_user_oauth_token(user["username"], token)
        except Exception as exc:
            return _oauth_error(str(exc))
        session.clear()
        session["user_id"] = user["username"]
        session.permanent = True
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    # ── login gate for everything below ──────────────────────────────────────

    @app.before_request
    def attach_user():
        g.user_id = session.get("user_id")

    def login_required(view: Callable):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not g.get("user_id"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Login required"}), 401
                return redirect(url_for("login_page"))
            return view(*args, **kwargs)
        return wrapped

    # ── pages ────────────────────────────────────────────────────────────────

    @app.get("/")
    @login_required
    def index():
        return render_template(
            "index.html",
            asset_version=_asset_version(),
            username=g.user_id,
            storage_limit_bytes=_storage_limit_bytes(),
        )

    # ── user / GitHub credentials API ────────────────────────────────────────

    @app.get("/api/me")
    @login_required
    def api_me():
        status = users.get_user_status(g.user_id)
        return jsonify(status)

    @app.get("/api/me/export")
    @login_required
    def api_export_account():
        status = users.get_user_status(g.user_id)
        export = {
            "account": status,
            "tasks": _list_tasks(g.user_id),
            "archives": [],
        }
        try:
            client = _user_client(g.user_id)
            export["archives"] = list_remote_archives(client=client)
        except Exception as exc:
            export["archive_error"] = str(exc)
        return jsonify(export)

    @app.delete("/api/me")
    @login_required
    def api_delete_account():
        try:
            _limit_user_action("delete-account", g.user_id)
        except RateLimitExceeded as exc:
            return jsonify({"error": str(exc)}), 429
        username = g.user_id
        users.delete_user(username)
        session.clear()
        return jsonify({"ok": True})

    @app.post("/api/me/credentials")
    @login_required
    def api_set_credentials():
        try:
            _limit_user_action("credentials", g.user_id)
        except RateLimitExceeded as exc:
            return jsonify({"error": str(exc)}), 429
        payload = request.get_json(force=True) or {}
        token = (payload.get("token") or "").strip()
        repo_slug = (payload.get("repo") or "").strip()
        saved_repo_slug = (payload.get("saved_repo") or "").strip()
        target_repo_slug = repo_slug or saved_repo_slug
        if not target_repo_slug or "/" not in target_repo_slug:
            return jsonify({"error": "repo is required (owner/repo)"}), 400
        try:
            owner, repo = parse_owner_repo(target_repo_slug)
            if not token:
                existing = users.get_user_credentials(g.user_id)
                token = (existing or {}).get("token", "")
            if not token:
                return jsonify({"error": "token is required"}), 400
            client = GitHubClient(token=token, owner=owner, repo=repo)
            login = client.viewer_login()
            if payload.get("create_repo") and repo_slug:
                client.ensure_repo(private=bool(payload.get("private_repo", True)))
            users.set_user_repo(g.user_id, target_repo_slug, token=token)
        except Exception as exc:
            return _credential_error_response(exc)
        return jsonify({"login": login, "repo": f"{owner}/{repo}", "repos": users.list_user_repos(g.user_id)})

    @app.delete("/api/me/credentials")
    @login_required
    def api_clear_credentials():
        users.clear_user_credentials(g.user_id)
        return jsonify({"ok": True})

    @app.post("/api/me/password")
    @login_required
    def api_change_password():
        payload = request.get_json(force=True) or {}
        current = payload.get("current_password") or ""
        new = payload.get("new_password") or ""
        if not users.verify_password(g.user_id, current):
            return jsonify({"error": "Current password is incorrect."}), 401
        try:
            users.change_password(g.user_id, new)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    # ── archives (per user) ──────────────────────────────────────────────────

    @app.get("/api/archives")
    @login_required
    def archives():
        try:
            client = _user_client(g.user_id)
            page = max(1, int(request.args.get("page", "1") or "1"))
            per_page = _archives_page_size(request.args.get("per_page"))
            result, has_more = list_remote_archives_page(page=page, per_page=per_page, client=client)
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except ValueError:
            return jsonify({"error": "page and per_page must be integers."}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "archives": result,
            "page": page,
            "per_page": per_page,
            "has_more": has_more,
        })

    @app.get("/api/archives/<int:release_id>/cover")
    @login_required
    def archive_cover(release_id: int):
        # Server-side cover generation has been removed: pulling a full asset
        # (potentially a multi-hundred-MB video) into RAM just to generate a
        # thumbnail is what was OOM-killing the 512 MB Render instance. We now
        # only serve a pre-uploaded `_cover.jpg` if one exists, and 404
        # otherwise so the front end falls back to a kind icon.
        from . import thumbnails
        try:
            client = _user_client(g.user_id)
            assets = client.list_release_assets(release_id)
        except Exception:
            abort(404)
        cover = next((a for a in assets if a["name"] == thumbnails.COVER_ASSET_NAME), None)
        if not cover:
            abort(404)
        try:
            data = client.download_asset_bytes(cover["id"], use_cache=True)
        except Exception:
            abort(404)
        return Response(
            data,
            mimetype="image/jpeg",
            headers={"Cache-Control": "private, max-age=600"},
        )

    @app.delete("/api/archives/<int:release_id>")
    @login_required
    def archive_delete(release_id: int):
        try:
            client = _user_client(g.user_id)
            result = delete_archive(release_id=release_id, client=client)
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(result)

    @app.get("/api/archives/<int:release_id>/contents")
    @login_required
    def archive_contents(release_id: int):
        try:
            client = _user_client(g.user_id)
            result = list_archive_contents(release_id=release_id, client=client)
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(result)

    @app.post("/api/archives/<int:release_id>/folders")
    @login_required
    def archive_folder_create(release_id: int):
        payload = request.get_json(force=True) or {}
        relative_path = _normalize_virtual_path(payload.get("path") or "")
        if not relative_path:
            return jsonify({"error": "path is required"}), 400
        try:
            client = _user_client(g.user_id)
            result = create_archive_folder(
                release_id=release_id,
                folder_path=relative_path,
                client=client,
            )
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(result)

    @app.post("/api/archives/folders")
    @login_required
    def archive_root_folder_create():
        payload = request.get_json(force=True) or {}
        folder_name = _browser_upload_source_name(payload.get("name") or "")
        if not folder_name:
            return jsonify({"error": "name is required"}), 400
        try:
            client = _user_client(g.user_id)
            result = create_empty_archive(
                source_name=folder_name,
                initial_folder_path="",
                private_release=bool(payload.get("private_release", False)),
                client=client,
            )
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(_serialize(result))

    @app.get("/api/archives/<int:release_id>/file")
    @login_required
    def archive_file(release_id: int):
        relative_path = (request.args.get("path") or "").strip()
        if not relative_path:
            return jsonify({"error": "path is required"}), 400
        try:
            client = _user_client(g.user_id)
            encode_key = _user_encode_key(g.user_id)
            entry_path, content_type, cleanup = fetch_archive_file_to_disk(
                release_id=release_id,
                relative_path=relative_path,
                encode_key=encode_key,
                client=client,
            )
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception:
            abort(404)

        @after_this_request
        def _cleanup_temp(response):
            cleanup()
            return response

        return send_file(
            entry_path,
            mimetype=content_type or "application/octet-stream",
            max_age=600,
        )

    @app.get("/api/archives/<int:release_id>/download-entry")
    @login_required
    def archive_entry_download(release_id: int):
        relative_path = _normalize_virtual_path(request.args.get("path") or "")
        if not relative_path:
            return jsonify({"error": "path is required"}), 400
        kind = (request.args.get("kind") or "file").strip().lower()
        if kind not in {"file", "folder"}:
            return jsonify({"error": "kind must be file or folder"}), 400
        try:
            client = _user_client(g.user_id)
            encode_key = _user_encode_key(g.user_id)
            if kind == "file":
                entry_path, content_type, cleanup = fetch_archive_file_to_disk(
                    release_id=release_id,
                    relative_path=relative_path,
                    encode_key=encode_key,
                    client=client,
                )

                @after_this_request
                def _cleanup_single(response):
                    cleanup()
                    return response

                return send_file(
                    entry_path,
                    mimetype=content_type or "application/octet-stream",
                    as_attachment=True,
                    download_name=Path(relative_path).name or "download",
                    max_age=0,
                )

            contents = list_archive_contents(release_id=release_id, client=client)
            prefix = f"{relative_path}/"
            matched_entries = [
                entry for entry in (contents.get("entries") or [])
                if (entry.get("relative_path") or "").startswith(prefix)
            ]
            folder_exists = any(
                (entry.get("kind") == "folder" and (entry.get("relative_path") or "") == relative_path)
                for entry in (contents.get("entries") or [])
            )
            if not matched_entries and not folder_exists:
                return jsonify({"error": f"Folder {relative_path!r} was not found in this archive."}), 404

            folder_name = Path(relative_path).name or "folder"
            temp_dir = Path(tempfile.mkdtemp(prefix="github-drive-entry-zip-"))
            zip_path = temp_dir / f"{_safe_download_name(folder_name)}.zip"
            # Stream every member through disk: download to a temp file, write
            # it into the zip via archive.write(), drop the temp file, then move
            # to the next entry. At any moment only one file is materialized,
            # so a 50-file folder doesn't accumulate 50 file payloads in RAM.
            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                if folder_exists and not matched_entries:
                    archive.writestr(f"{folder_name}/", b"")
                for entry in matched_entries:
                    entry_path_value = entry.get("relative_path") or ""
                    remainder = entry_path_value[len(prefix):].lstrip("/")
                    if not remainder:
                        continue
                    member_path, _content_type, member_cleanup = fetch_archive_file_to_disk(
                        release_id=release_id,
                        relative_path=entry_path_value,
                        encode_key=encode_key,
                        client=client,
                    )
                    try:
                        archive.write(member_path, arcname=f"{folder_name}/{remainder}")
                    finally:
                        member_cleanup()

            response = send_file(
                zip_path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=zip_path.name,
                max_age=0,
            )
            response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
            return response
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.delete("/api/archives/<int:release_id>/files")
    @login_required
    def archive_file_delete(release_id: int):
        payload = request.get_json(force=True) or {}
        relative_path = (payload.get("relative_path") or "").strip()
        if not relative_path:
            return jsonify({"error": "relative_path is required"}), 400
        try:
            client = _user_client(g.user_id)
            encode_key = _user_encode_key(g.user_id)
            result = delete_archive_file(
                release_id=release_id,
                relative_path=relative_path,
                encode_key=encode_key,
                client=client,
            )
        except RuntimeError as exc:
            return _credential_error_response(exc)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(result)

    # ── tasks (per user) ─────────────────────────────────────────────────────

    @app.get("/api/tasks")
    @login_required
    def tasks():
        return jsonify({"tasks": _list_tasks(g.user_id)})

    @app.get("/api/tasks/<task_id>")
    @login_required
    def task(task_id: str):
        _repair_orphaned_tasks(g.user_id)
        task_data = _get_task(task_id, g.user_id)
        if task_data is None:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task_data)

    @app.post("/api/abuse-report")
    @login_required
    def abuse_report():
        try:
            _limit_user_action("abuse-report", g.user_id)
        except RateLimitExceeded as exc:
            return jsonify({"error": str(exc)}), 429
        payload = request.get_json(force=True) or {}
        subject = str(payload.get("subject") or "").strip()[:160]
        details = str(payload.get("details") or "").strip()[:4000]
        if not subject or not details:
            return jsonify({"error": "subject and details are required"}), 400
        report_id = uuid.uuid4().hex[:12]
        moderation.save_abuse_report({
            "id": report_id,
            "reporter": g.user_id,
            "subject": subject,
            "details": details,
        })
        return jsonify({"ok": True, "report_id": report_id})

    @app.get("/api/admin/users")
    @login_required
    def admin_users():
        if not _is_admin(g.user_id):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"users": users.list_users()})

    @app.delete("/api/admin/users/<username>")
    @login_required
    def admin_delete_user(username: str):
        if not _is_admin(g.user_id):
            return jsonify({"error": "Not found"}), 404
        if users.normalize_username(username) == users.normalize_username(g.user_id):
            return jsonify({"error": "You cannot delete your own account from this endpoint."}), 400
        deleted = users.delete_user(username)
        return jsonify({"deleted": bool(deleted)})

    # ── upload (per user) ────────────────────────────────────────────────────

    @app.post("/api/upload-files")
    @login_required
    def upload_files():
        try:
            _limit_user_action("upload", g.user_id)
            _enforce_active_task_limit(g.user_id, "upload")
            creds = users.get_user_credentials(g.user_id)
        except RateLimitExceeded as exc:
            return jsonify({"error": str(exc)}), 429
        except RuntimeError as exc:
            return _credential_error_response(exc)
        if not creds:
            return jsonify({"error": "Configure your GitHub token first."}), 400

        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            return jsonify({"error": "No files were uploaded"}), 400
        if len(uploaded_files) > _max_files_per_upload():
            return jsonify({"error": f"Too many files in one upload. Limit is {_max_files_per_upload()}."}), 413

        relative_paths = request.form.getlist("relative_paths")
        if relative_paths and len(relative_paths) != len(uploaded_files):
            return jsonify({"error": "relative_paths count does not match uploaded files"}), 400
        requested_source_name = _browser_upload_source_name(request.form.get("source_name_override") or "")
        requested_source_type = (request.form.get("source_type_override") or "").strip().lower()
        if requested_source_type not in {"file", "directory"}:
            requested_source_type = None
        encrypt = request.form.get("encrypt", "false").lower() == "true"
        private_release = request.form.get("private_release", "false").lower() == "true"
        retries = int(request.form.get("retries", 3))
        append_tag = (request.form.get("append_tag") or "").strip() or None
        append_relative_path = _normalize_virtual_path(request.form.get("append_relative_path") or "") or None

        if len(uploaded_files) == 1:
            uploaded_file = uploaded_files[0]
            raw_relative_path = relative_paths[0] if relative_paths else uploaded_file.filename
            try:
                relative_path = _safe_upload_relative_path(raw_relative_path, uploaded_file.filename or "upload")
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            size_bytes = _uploaded_file_size(uploaded_file)
            if size_bytes > _max_user_upload_bytes():
                return jsonify({"error": f"Upload exceeds the per-upload limit of {_max_user_upload_bytes()} bytes."}), 413
            direct_upload_ok = (
                not encrypt
                and not append_tag
                and not append_relative_path
                and "/" not in relative_path
                and size_bytes > 0
            )
            if direct_upload_ok:
                payload = {
                    "source_path": relative_path,
                    "private_release": private_release,
                    "retries": retries,
                    "encrypt": False,
                    "upload_origin": "browser-transfer",
                    "uploaded_file_count": 1,
                    "browser_display_name": Path(relative_path).name or "Upload",
                    "browser_direct_upload": True,
                }
                task_id = _create_task("upload", g.user_id, payload, initial_status="running")
                try:
                    client = _user_client(g.user_id)
                    result = upload_browser_single_file(
                        file_stream=uploaded_file.stream,
                        relative_path=relative_path,
                        size_bytes=size_bytes,
                        content_type=(uploaded_file.mimetype or None),
                        display_name=requested_source_name or Path(relative_path).name,
                        private_release=private_release,
                        retries=retries,
                        progress=lambda event, progress_payload: _task_progress(task_id, event, progress_payload),
                        client=client,
                    )
                    _update_task(task_id, status="completed", result=_serialize(result))
                except Exception as exc:
                    _update_task(task_id, status="failed", error=str(exc))
                return jsonify({"task_id": task_id})

        staging_root = Path(tempfile.mkdtemp(prefix="github-drive-web-upload-"))
        saved_paths = []
        saved_bytes = 0
        for index, uploaded_file in enumerate(uploaded_files):
            raw_relative_path = relative_paths[index] if index < len(relative_paths) else uploaded_file.filename
            try:
                relative_path = _safe_upload_relative_path(raw_relative_path, uploaded_file.filename or f"upload-{index}")
            except ValueError as exc:
                shutil.rmtree(staging_root, ignore_errors=True)
                return jsonify({"error": str(exc)}), 400
            destination = staging_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            uploaded_file.save(destination)
            saved_bytes += destination.stat().st_size
            if saved_bytes > _max_user_upload_bytes():
                shutil.rmtree(staging_root, ignore_errors=True)
                return jsonify({"error": f"Upload exceeds the per-upload limit of {_max_user_upload_bytes()} bytes."}), 413
            saved_paths.append(destination)

        safe_relative_paths = [str(path.relative_to(staging_root)) for path in saved_paths]
        folder_root = _browser_upload_root_folder(safe_relative_paths)
        source_name_override = None
        source_type_override = None
        if len(saved_paths) == 1 and not any(path and ("/" in path or "\\" in path) for path in safe_relative_paths):
            source_path = str(saved_paths[0])
            if requested_source_name:
                source_name_override = requested_source_name
                source_type_override = requested_source_type or "file"
        elif folder_root:
            source_path = str(staging_root / folder_root)
            source_name_override = folder_root
            source_type_override = "directory"
        else:
            source_path = str(staging_root)
            if requested_source_name:
                source_name_override = requested_source_name
                source_type_override = requested_source_type or "directory"

        payload = {
            "source_path": source_path,
            "private_release": private_release,
            "workers": int(request.form.get("workers", 2)),
            "recursive": request.form.get("recursive", "true").lower() == "true",
            "retries": retries,
            "encrypt": encrypt,
            "upload_mode": (request.form.get("upload_mode") or "auto").strip().lower() or "auto",
            "resume_tag": (request.form.get("resume_tag") or "").strip() or None,
            "append_tag": append_tag,
            "append_relative_path": append_relative_path,
            "cleanup_staging_root": str(staging_root),
            "upload_origin": "browser-transfer",
            "uploaded_file_count": len(saved_paths),
        }
        if len(saved_paths) == 1:
            payload["browser_display_name"] = Path(safe_relative_paths[0]).name or "Upload"
        elif folder_root:
            payload["browser_display_name"] = folder_root
        elif requested_source_name:
            payload["browser_display_name"] = requested_source_name
        else:
            payload["browser_display_name"] = f"Selection - {len(saved_paths)} files"
        if source_name_override:
            payload["source_name_override"] = source_name_override
        if source_type_override:
            payload["source_type_override"] = source_type_override
        task_id = _create_task("upload", g.user_id, payload)
        _start_task_thread(task_id, _run_upload_task)
        return jsonify({"task_id": task_id})

    # ── download (per user) ──────────────────────────────────────────────────

    @app.post("/api/download")
    @login_required
    def download():
        try:
            _limit_user_action("download", g.user_id)
            _enforce_active_task_limit(g.user_id, "download")
            creds = users.get_user_credentials(g.user_id)
        except RateLimitExceeded as exc:
            return jsonify({"error": str(exc)}), 429
        except RuntimeError as exc:
            return _credential_error_response(exc)
        if not creds:
            return jsonify({"error": "Configure your GitHub token first."}), 400

        payload = request.get_json(force=True) or {}
        release_id = payload.get("release_id")
        tag = (payload.get("tag") or "").strip() or None
        archive_id = (payload.get("archive_id") or "").strip() or None
        if not (release_id or tag or archive_id):
            return jsonify({"error": "release_id, tag, or archive_id is required"}), 400

        temp_dir = tempfile.mkdtemp(prefix="github-drive-dl-")
        task_id = _create_task(
            "download",
            g.user_id,
            {
                "release_id": int(release_id) if release_id else None,
                "tag": tag,
                "archive_id": archive_id,
                "destination_dir": temp_dir,
                "workers": int(payload.get("workers", 2)),
                "skip_existing": False,
                "retries": int(payload.get("retries", 3)),
            },
        )
        with _DOWNLOAD_LOCK:
            _DOWNLOAD_DIRS[task_id] = temp_dir

        _start_task_thread(task_id, _run_download_task)
        return jsonify({"task_id": task_id})

    @app.get("/api/download-file/<task_id>")
    @login_required
    def download_file(task_id: str):
        task = _get_task(task_id, g.user_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        if task["status"] != "completed":
            return jsonify({"error": "Task not yet completed"}), 409

        with _DOWNLOAD_LOCK:
            temp_dir = _DOWNLOAD_DIRS.get(task_id)
        if not temp_dir or not Path(temp_dir).exists():
            return jsonify({"error": "Download data is no longer available. Please re-download."}), 410

        archive_title = (task.get("result") or {}).get("title") or "archive"
        safe_name = re.sub(r"[^\w\s\-.]", "_", archive_title)[:80]
        zip_dir = tempfile.mkdtemp(prefix="github-drive-web-zip-")
        zip_path = Path(zip_dir) / f"{safe_name}.zip"
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            base = Path(temp_dir)
            for file_path in sorted(base.rglob("*")):
                if file_path.is_file():
                    zf.write(str(file_path), arcname=str(file_path.relative_to(base)))

        def cleanup() -> None:
            with _DOWNLOAD_LOCK:
                _DOWNLOAD_DIRS.pop(task_id, None)
            shutil.rmtree(temp_dir, ignore_errors=True)
            shutil.rmtree(zip_dir, ignore_errors=True)

        response = send_file(
            str(zip_path),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{safe_name}.zip",
        )
        response.call_on_close(cleanup)
        return response

    return app


# ── per-user GitHub client + encryption key resolution ───────────────────────


def _user_client(username: str) -> GitHubClient:
    creds = users.get_user_credentials(username)
    if not creds:
        raise RuntimeError("Configure your GitHub token first.")
    if not creds.get("token") or not creds.get("owner") or not creds.get("repo"):
        raise RuntimeError("Configure your GitHub token first.")
    return GitHubClient(token=creds["token"], owner=creds["owner"], repo=creds["repo"])


def _user_encode_key(username: str) -> Optional[bytes]:
    return users.derive_user_archive_key(username)


# ── task runners ──────────────────────────────────────────────────────────────


def _run_upload_task(task_id: str) -> None:
    task = _get_task_internal(task_id)
    if task is None:
        return
    user_id = task.get("user_id") or ""
    payload = dict(task["payload"])
    cleanup_staging_root = payload.pop("cleanup_staging_root", None)
    payload.pop("upload_origin", None)
    payload.pop("uploaded_file_count", None)
    payload.pop("browser_display_name", None)
    encrypt = bool(payload.get("encrypt", False))
    # Flip to "running" before any blocking calls so the UI doesn't sit on QUEUED
    # while we open a DB connection / decrypt the stored PAT.
    if str(task.get("status") or "") != "running":
        _update_task(task_id, status="running")
    try:
        client = _user_client(user_id)
    except Exception as exc:
        _update_task(task_id, status="failed", error=str(exc))
        if cleanup_staging_root:
            shutil.rmtree(cleanup_staging_root, ignore_errors=True)
        return
    if encrypt:
        try:
            payload["encode_key"] = _user_encode_key(user_id)
        except Exception as exc:
            _update_task(task_id, status="failed", error=str(exc))
            if cleanup_staging_root:
                shutil.rmtree(cleanup_staging_root, ignore_errors=True)
            return
    try:
        append_tag = payload.pop("append_tag", None)
        append_relative_path = payload.pop("append_relative_path", None)
        if append_tag:
            # Append mode targets an existing archive, so creation/resume-only knobs from the
            # generic upload payload must not be forwarded to append_to_archive().
            payload.pop("private_release", None)
            payload.pop("upload_mode", None)
            payload.pop("source_name_override", None)
            payload.pop("source_type_override", None)
            payload.pop("resume_release_id", None)
            payload.pop("resume_tag", None)
            payload.pop("resume_archive_id", None)
            result = append_to_archive(
                client=client,
                progress=lambda event, data: _task_progress(task_id, event, data),
                tag=append_tag,
                base_relative_path=append_relative_path or "",
                **payload,
            )
        else:
            result = upload_archive(
                client=client,
                progress=lambda event, data: _task_progress(task_id, event, data),
                **payload,
            )
        _update_task(task_id, status="completed", result=_serialize(result))
    except Exception as exc:
        _update_task(task_id, status="failed", error=str(exc))
    finally:
        if cleanup_staging_root:
            shutil.rmtree(cleanup_staging_root, ignore_errors=True)


def _run_download_task(task_id: str) -> None:
    task = _get_task_internal(task_id)
    if task is None:
        return
    user_id = task.get("user_id") or ""
    payload = dict(task["payload"])
    if str(task.get("status") or "") != "running":
        _update_task(task_id, status="running")
    try:
        client = _user_client(user_id)
    except Exception as exc:
        _update_task(task_id, status="failed", error=str(exc))
        with _DOWNLOAD_LOCK:
            temp_dir = _DOWNLOAD_DIRS.pop(task_id, None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return
    try:
        try:
            payload["encode_key"] = _user_encode_key(user_id)
        except Exception:
            payload["encode_key"] = None
        result = download_archive(
            client=client,
            progress=lambda event, data: _task_progress(task_id, event, data),
            **payload,
        )
        _update_task(task_id, status="completed", result=_serialize(result))
    except Exception as exc:
        _update_task(task_id, status="failed", error=str(exc))
        with _DOWNLOAD_LOCK:
            temp_dir = _DOWNLOAD_DIRS.pop(task_id, None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ── task helpers (per-user scoped) ────────────────────────────────────────────


def _create_task(task_type: str, user_id: str, payload: Dict[str, Any], initial_status: str = "queued") -> str:
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "type": task_type,
        "user_id": user_id,
        "status": initial_status,
        "created_at": time.time(),
        "updated_at": time.time(),
        "payload": payload,
        "logs": [],
        "result": None,
        "error": None,
        "progress_total": 0,
        "progress_done": 0,
    }
    task_store.create_task(task)
    with _TASK_LOCK:
        _TASK_CACHE[task_id] = _clone_task(task)
        _mark_task_flushed(task_id, task)
    return task_id


def _start_task_thread(task_id: str, runner) -> None:
    global _TASK_ACTIVE_RUNNERS
    max_active = _global_active_task_limit()
    if max_active <= 0:
        # No global cap: this task will run immediately, so skip the visible
        # "queued" state and reflect that in the task row before we return.
        _update_task(task_id, status="running")
        _launch_task_runner(task_id, runner, counted=False)
        return
    _ensure_task_dispatcher()
    # Fast path: if a runner slot is free, take it now and launch directly
    # instead of paying the dispatcher's wake-up latency. We also flip the
    # task to "running" synchronously so the very next /api/tasks poll from
    # the browser shows "running" instead of "queued" — this is what the
    # direct single-file upload path already does, applied uniformly here.
    with _TASK_QUEUE_COND:
        if not _TASK_QUEUE and _TASK_ACTIVE_RUNNERS < max_active:
            _TASK_ACTIVE_RUNNERS += 1
            launch_now = True
        else:
            _TASK_QUEUE.append({"task_id": task_id, "runner": runner})
            _TASK_QUEUE_COND.notify_all()
            launch_now = False
    if launch_now:
        _update_task(task_id, status="running")
        _launch_task_runner(task_id, runner, counted=True)


def _get_task_internal(task_id: str) -> Optional[Dict]:
    with _TASK_LOCK:
        cached = _TASK_CACHE.get(task_id)
        if cached is not None:
            return _serialize(_clone_task(cached))
    try:
        task = task_store.get_task(task_id)
    except Exception as exc:
        from . import db as db_module

        if isinstance(exc, db_module.DatabaseUnavailableError):
            with _TASK_LOCK:
                cached = _TASK_CACHE.get(task_id)
                if cached is not None:
                    return _serialize(_clone_task(cached))
        raise
    if not task:
        return None
    with _TASK_LOCK:
        _TASK_CACHE.setdefault(task_id, _clone_task(task))
        cached = _TASK_CACHE[task_id]
    return _serialize(_clone_task(cached))


def _get_task(task_id: str, user_id: str) -> Optional[Dict]:
    task = _get_task_internal(task_id)
    if task is None:
        return None
    if task.get("user_id") != user_id:
        return None
    return task


def _list_tasks(user_id: str) -> List[Dict]:
    _repair_orphaned_tasks(user_id)
    persisted: List[Dict[str, Any]] = []
    db_busy = False
    try:
        persisted = [_clone_task(t) for t in task_store.list_tasks(user_id)]
    except Exception as exc:
        from . import db as db_module

        if isinstance(exc, db_module.DatabaseUnavailableError):
            db_busy = True
            LOG.warning("Serving cached task list for %s because DB is busy.", user_id)
        else:
            raise
    merged = {task["id"]: task for task in persisted}
    with _TASK_LOCK:
        cached_tasks = [
            _clone_task(task)
            for task in _TASK_CACHE.values()
            if task.get("user_id") == user_id
        ]
    for task in cached_tasks:
        merged[task["id"]] = task
    if db_busy and not merged:
        return []
    rows = sorted(merged.values(), key=lambda task: float(task.get("created_at", 0) or 0), reverse=True)
    return [_serialize(task) for task in rows]


def _update_task(task_id: str, **updates) -> None:
    now = time.time()
    updates["updated_at"] = now
    task = _get_or_load_task(task_id)
    if task is None:
        return
    with _TASK_LOCK:
        cached = _TASK_CACHE.setdefault(task_id, _clone_task(task))
        cached.update(updates)
        snapshot = _clone_task(cached)
        _TASK_CACHE_META.setdefault(task_id, {})["dirty"] = True
    if _persist_task_updates(task_id, updates, tolerate_busy=True):
        with _TASK_LOCK:
            _mark_task_flushed(task_id, snapshot)


def _task_progress(task_id: str, event: str, payload: Dict[str, Any]) -> None:
    message = _format_progress_message(event, payload)
    now = time.time()
    with _TASK_LOCK:
        task = _TASK_CACHE.get(task_id)
    if task is None:
        task = _get_or_load_task(task_id)
        if task is None:
            return
    with _TASK_LOCK:
        task = _TASK_CACHE.setdefault(task_id, _clone_task(task))
        task["last_event"] = event
        if event in {"archive_created", "archive_downloading"}:
            task["progress_total"] = int(payload.get("total_items", 0))
            task["progress_done"] = 0
        elif event == "archive_resumed":
            task["progress_total"] = int(payload.get("total_items", 0))
            task["progress_done"] = int(payload.get("completed_items", 0))
        elif event in {"item_uploaded", "item_downloaded", "item_skipped"}:
            increment = int(payload.get("progress_increment", 1) or 1)
            task["progress_done"] = int(task.get("progress_done", 0)) + increment
        logs = list(task.get("logs") or [])
        logs.append({
            "timestamp": now,
            "event": event,
            "message": message,
            "payload": payload,
        })
        task["updated_at"] = now
        task["logs"] = logs[-200:]
        _TASK_CACHE_META.setdefault(task_id, {})["dirty"] = True
        snapshot = _clone_task(task)
        should_flush = _should_flush_task_progress(task_id, snapshot, event, now)
    if should_flush and _persist_task_updates(task_id, {
            "updated_at": now,
            "last_event": task.get("last_event"),
            "progress_total": task.get("progress_total", 0),
            "progress_done": task.get("progress_done", 0),
            "logs": snapshot.get("logs", []),
        }, tolerate_busy=True):
        with _TASK_LOCK:
            _mark_task_flushed(task_id, snapshot)


def _format_progress_message(event: str, payload: Dict[str, Any]) -> str:
    if event == "archive_created":
        return f"Created release {payload.get('tag') or payload.get('release_id')}"
    if event == "archive_resumed":
        return f"Resumed release {payload.get('release_id')} with {payload['completed_items']} completed item(s)"
    if event == "archive_uploaded":
        return f"Uploaded {payload['total_items']} item(s)"
    if event == "archive_downloading":
        return f"Downloading {payload['total_items']} item(s)"
    if event == "archive_downloaded":
        return f"Downloaded {payload['downloaded_items']} item(s)"
    if event == "item_preparing":
        return f"Preparing {payload['relative_path']}"
    if event == "item_uploaded":
        return f"Uploaded {payload['relative_path']}"
    if event == "item_downloading":
        return f"Downloading {payload['relative_path']}"
    if event == "item_downloaded":
        return f"Saved {payload['relative_path']}"
    if event == "item_skipped":
        return f"Skipped {payload['relative_path']}"
    return f"{event}: {json.dumps(payload, ensure_ascii=True, default=str)}"


def _clone_task(task: Dict[str, Any]) -> Dict[str, Any]:
    cloned = dict(task)
    cloned["payload"] = dict(task.get("payload") or {})
    cloned["logs"] = list(task.get("logs") or [])
    return cloned


def _get_or_load_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _TASK_LOCK:
        cached = _TASK_CACHE.get(task_id)
        if cached is not None:
            return _clone_task(cached)
    try:
        task = task_store.get_task(task_id)
    except Exception as exc:
        from . import db as db_module

        if isinstance(exc, db_module.DatabaseUnavailableError):
            with _TASK_LOCK:
                cached = _TASK_CACHE.get(task_id)
                if cached is not None:
                    return _clone_task(cached)
        raise
    if not task:
        return None
    task = _clone_task(task)
    with _TASK_LOCK:
        _TASK_CACHE.setdefault(task_id, _clone_task(task))
    return task


def _persist_task_updates(task_id: str, updates: Dict[str, Any], tolerate_busy: bool) -> bool:
    try:
        task_store.update_task(task_id, updates)
        return True
    except Exception as exc:
        from . import db as db_module

        if tolerate_busy and isinstance(exc, db_module.DatabaseUnavailableError):
            LOG.warning("Deferred task persistence for %s: %s", task_id, exc)
            return False
        raise


def _mark_task_flushed(task_id: str, task: Dict[str, Any]) -> None:
    meta = _TASK_CACHE_META.setdefault(task_id, {})
    meta["last_flush_at"] = float(task.get("updated_at") or time.time())
    meta["last_flush_done"] = int(task.get("progress_done") or 0)
    meta["dirty"] = False


def _should_flush_task_progress(task_id: str, task: Dict[str, Any], event: str, now: float) -> bool:
    if event not in {"item_uploaded", "item_downloaded", "item_skipped"}:
        return True
    meta = _TASK_CACHE_META.setdefault(task_id, {})
    last_flush_at = float(meta.get("last_flush_at") or 0.0)
    last_flush_done = int(meta.get("last_flush_done") or 0)
    done = int(task.get("progress_done") or 0)
    total = int(task.get("progress_total") or 0)
    if total > 0 and done >= total:
        return True
    if done - last_flush_done >= _TASK_PROGRESS_FLUSH_ITEMS:
        return True
    return (now - last_flush_at) >= _TASK_PROGRESS_FLUSH_SECONDS


def _serialize(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


def _asset_version() -> str:
    static_dir = Path(__file__).parent / "static"
    try:
        latest = max(p.stat().st_mtime for p in static_dir.iterdir() if p.is_file())
    except (OSError, ValueError):
        return "0"
    return str(int(latest))


def _signup_disabled_response():
    message = "Signup is disabled on this server. Contact the administrator to create an account."
    if _github_oauth_enabled() and _github_oauth_signup_enabled():
        message = "Create your account with GitHub on this server. Password signup is disabled."
    return _render_auth_page(mode="login", error=message, status=403)


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _csrf_from_request() -> str:
    return (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or ""
    )


def _client_key() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _limit_auth_attempt(bucket: str) -> None:
    check_rate_limit(
        bucket,
        _client_key(),
        env_int("GITHUB_DRIVE_AUTH_RATE_LIMIT", 20),
        env_int("GITHUB_DRIVE_AUTH_RATE_WINDOW_SECONDS", 15 * 60),
    )


def _limit_user_action(bucket: str, user_id: str) -> None:
    check_rate_limit(
        bucket,
        f"{user_id}:{_client_key()}",
        env_int("GITHUB_DRIVE_USER_ACTION_RATE_LIMIT", 60),
        env_int("GITHUB_DRIVE_USER_ACTION_RATE_WINDOW_SECONDS", 60),
    )


def _active_task_count(user_id: str, task_type: Optional[str] = None) -> int:
    tasks = _list_tasks(user_id)
    return sum(
        1
        for task in tasks
        if task.get("status") in {"queued", "running"}
        and (task_type is None or task.get("type") == task_type)
    )


def _enforce_active_task_limit(user_id: str, task_type: str) -> None:
    max_active = env_int("GITHUB_DRIVE_MAX_ACTIVE_TASKS_PER_USER", 1)
    if max_active > 0 and _active_task_count(user_id) >= max_active:
        raise RateLimitExceeded(f"You already have {max_active} active transfer(s). Wait for one to finish first.")
    per_type = env_int(f"GITHUB_DRIVE_MAX_ACTIVE_{task_type.upper()}S_PER_USER", 1)
    if per_type > 0 and _active_task_count(user_id, task_type=task_type) >= per_type:
        raise RateLimitExceeded(f"You already have {per_type} active {task_type} task(s).")


def _credential_error_response(exc: Exception):
    message = str(exc)
    if "Could not decrypt stored PAT" in message:
        return jsonify({
            "error": "Your saved GitHub token can no longer be decrypted. Please re-enter it.",
            "credential_recovery_required": True,
        }), 409
    return jsonify({"error": message}), 400


def _is_admin(username: str) -> bool:
    configured = {
        users.normalize_username(item)
        for item in re.split(r"[,\s]+", os.environ.get("GITHUB_DRIVE_ADMIN_USERS", ""))
        if item.strip()
    }
    return users.normalize_username(username) in configured


def _github_oauth_enabled() -> bool:
    return bool(
        (os.environ.get("GITHUB_OAUTH_CLIENT_ID") or "").strip()
        and (os.environ.get("GITHUB_OAUTH_CLIENT_SECRET") or "").strip()
    )


def _github_oauth_signup_enabled() -> bool:
    raw = os.environ.get("GITHUB_DRIVE_ALLOW_GITHUB_OAUTH_SIGNUP")
    if raw is None or not raw.strip():
        return users.signup_enabled()
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _github_oauth_redirect_uri() -> str:
    configured = (os.environ.get("GITHUB_OAUTH_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    if request.is_secure:
        return url_for("github_oauth_callback", _external=True, _scheme="https")
    return url_for("github_oauth_callback", _external=True)


def _github_oauth_exchange_code(code: str) -> str:
    import requests

    response = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": os.environ["GITHUB_OAUTH_CLIENT_ID"].strip(),
            "client_secret": os.environ["GITHUB_OAUTH_CLIENT_SECRET"].strip(),
            "code": code,
            "redirect_uri": _github_oauth_redirect_uri(),
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload.get("error_description") or payload["error"])
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("GitHub did not return an access token.")
    return token


def _github_oauth_profile(token: str) -> Dict[str, Any]:
    import requests

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-drive",
    }
    response = requests.get("https://api.github.com/user", headers=headers, timeout=20)
    response.raise_for_status()
    profile = response.json()
    if not profile.get("id") or not profile.get("login"):
        raise RuntimeError("GitHub profile response was incomplete.")
    if not profile.get("email"):
        try:
            emails = requests.get("https://api.github.com/user/emails", headers=headers, timeout=20)
            if emails.ok:
                primary = next((item for item in emails.json() if item.get("primary")), None)
                if primary:
                    profile["email"] = primary.get("email") or ""
        except Exception:
            pass
    return profile


def _oauth_error(message: str):
    return _render_auth_page(mode="login", error=message, status=400)


def _render_auth_page(mode: str, error: Optional[str] = None, status: int = 200):
    response = render_template(
        "login.html",
        asset_version=_asset_version(),
        allow_signup=users.signup_enabled(),
        github_oauth_enabled=_github_oauth_enabled(),
        github_oauth_signup_enabled=_github_oauth_signup_enabled(),
        turnstile_enabled=_turnstile_enabled(),
        turnstile_site_key=(os.environ.get("GITHUB_DRIVE_TURNSTILE_SITE_KEY") or "").strip(),
        mode=mode,
        error=error,
    )
    return response, status


def _turnstile_enabled() -> bool:
    return bool(
        (os.environ.get("GITHUB_DRIVE_TURNSTILE_SITE_KEY") or "").strip()
        and (os.environ.get("GITHUB_DRIVE_TURNSTILE_SECRET_KEY") or "").strip()
    )


def _verify_turnstile(expected_action: str) -> Optional[str]:
    import requests

    token = (request.form.get("cf-turnstile-response") or "").strip()
    if not token:
        return "Complete the verification check and try again."
    try:
        response = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": os.environ["GITHUB_DRIVE_TURNSTILE_SECRET_KEY"].strip(),
                "response": token,
                "remoteip": _client_ip(),
                "idempotency_key": str(uuid.uuid4()),
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        LOG.warning("Turnstile validation failed: %s", exc)
        return "Human verification is temporarily unavailable. Please try again."
    if not payload.get("success"):
        LOG.warning("Turnstile rejected auth request: %s", payload.get("error-codes") or [])
        return "Verification failed or expired. Please try again."
    returned_action = str(payload.get("action") or "").strip()
    if expected_action and returned_action and returned_action != expected_action:
        LOG.warning("Turnstile action mismatch: expected %s, got %s", expected_action, returned_action)
        return "Verification could not be confirmed. Please try again."
    return None


def _client_ip() -> str:
    forwarded = (request.headers.get("CF-Connecting-IP") or "").strip()
    if forwarded:
        return forwarded
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _global_active_task_limit() -> int:
    return env_int("GITHUB_DRIVE_MAX_ACTIVE_TASKS_GLOBAL", 1)


def _queued_task_ids() -> Set[str]:
    with _TASK_QUEUE_COND:
        return {str(item.get("task_id") or "") for item in _TASK_QUEUE if item.get("task_id")}


def _running_task_ids() -> Set[str]:
    with _TASK_QUEUE_COND:
        return set(_TASK_ACTIVE_IDS)


def _repair_orphaned_tasks(user_id: str) -> None:
    now = time.time()
    try:
        tasks = task_store.list_tasks(user_id, limit=100)
    except Exception:
        return
    queued_ids = _queued_task_ids()
    running_ids = _running_task_ids()
    for task in tasks:
        status = str(task.get("status") or "")
        if status not in {"queued", "running"}:
            continue
        payload = task.get("payload") or {}
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        if status == "queued" and task_id not in queued_ids:
            message = "This transfer was stuck in the server queue and has been cleared. Please retry."
            if payload.get("browser_direct_upload"):
                message = "This direct browser upload never started on the server. Please retry."
            _update_task(task_id, status="failed", error=message)
            continue
        age = now - float(task.get("created_at") or 0.0)
        grace = 5.0 if payload.get("browser_direct_upload") and status == "queued" else _TASK_ORPHAN_GRACE_SECONDS
        if age < grace:
            continue
        if payload.get("browser_direct_upload") and status == "queued":
            _update_task(
                task_id,
                status="failed",
                error="This direct browser upload never started on the server. Please retry.",
            )
            continue
        if status == "queued" and task_id in queued_ids:
            continue
        if status == "running" and task_id in running_ids:
            continue
        _update_task(
            task_id,
            status="failed",
            error="This transfer was stuck in the server queue and has been cleared. Please retry.",
        )


def _ensure_task_dispatcher() -> None:
    global _TASK_DISPATCHER_STARTED
    with _TASK_QUEUE_COND:
        if _TASK_DISPATCHER_STARTED:
            return
        thread = threading.Thread(target=_task_dispatcher_loop, daemon=True, name="github-drive-task-dispatcher")
        thread.start()
        _TASK_DISPATCHER_STARTED = True
        LOG.info("task dispatcher started (pid=%s)", os.getpid())


def _task_dispatcher_loop() -> None:
    global _TASK_ACTIVE_RUNNERS

    while True:
        try:
            with _TASK_QUEUE_COND:
                while True:
                    limit = max(1, _global_active_task_limit())
                    if _TASK_QUEUE and _TASK_ACTIVE_RUNNERS < limit:
                        queued = _TASK_QUEUE.pop(0)
                        _TASK_ACTIVE_RUNNERS += 1
                        break
                    _TASK_QUEUE_COND.wait(timeout=1.0)
            LOG.info(
                "dispatching task %s (active=%s queue=%s)",
                queued.get("task_id"),
                _TASK_ACTIVE_RUNNERS,
                len(_TASK_QUEUE),
            )
            _update_task(queued["task_id"], status="running")
            _launch_task_runner(queued["task_id"], queued["runner"], counted=True)
        except Exception:
            LOG.exception("task dispatcher iteration failed; continuing")


def _launch_task_runner(task_id: str, runner, counted: bool) -> None:
    def invoke() -> None:
        global _TASK_ACTIVE_RUNNERS
        with _TASK_QUEUE_COND:
            _TASK_ACTIVE_IDS.add(task_id)
        try:
            runner(task_id)
        finally:
            with _TASK_QUEUE_COND:
                _TASK_ACTIVE_IDS.discard(task_id)
                if counted:
                    _TASK_ACTIVE_RUNNERS = max(0, _TASK_ACTIVE_RUNNERS - 1)
                _TASK_QUEUE_COND.notify_all()

    thread = threading.Thread(target=invoke, daemon=True, name=f"github-drive-task-{task_id}")
    thread.start()


# ── hosting helpers ──────────────────────────────────────────────────────────


def _max_upload_bytes() -> int:
    raw = os.environ.get("GITHUB_DRIVE_MAX_UPLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


def _max_user_upload_bytes() -> int:
    return env_int("GITHUB_DRIVE_USER_MAX_UPLOAD_BYTES", DEFAULT_USER_UPLOAD_BYTES)


def _max_files_per_upload() -> int:
    return env_int("GITHUB_DRIVE_MAX_FILES_PER_UPLOAD", 5000)


def _cookie_secure_default() -> bool:
    """Mark the session cookie Secure on production-like hosts. Local dev stays http."""
    if (os.environ.get("GITHUB_DRIVE_FORCE_SECURE_COOKIES") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    if os.environ.get("RENDER") or os.environ.get("DYNO") or os.environ.get("FLY_APP_NAME"):
        return True
    return False


def _read_basic_auth_credentials() -> Optional[tuple]:
    raw = (os.environ.get("GITHUB_DRIVE_BASIC_AUTH") or "").strip()
    if not raw or ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    user = user.strip()
    if not user or not password:
        return None
    return (user, password)


def _make_basic_auth_guard(expected: tuple):
    expected_user, expected_password = expected

    def guard():
        if request.path in {"/healthz", "/warm-db"}:
            return None
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[len("Basic "):], validate=True).decode("utf-8")
                user, _, password = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                user = password = ""
            if secrets.compare_digest(user, expected_user) and secrets.compare_digest(password, expected_password):
                return None
        return Response(
            "Authentication required.",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="github-drive", charset="UTF-8"'},
        )

    return guard


def _warm_db_token() -> str:
    return (
        (request.args.get("token") or "").strip()
        or (request.headers.get("X-Warm-Token") or "").strip()
    )


def _browser_upload_root_folder(relative_paths: List[str]) -> Optional[str]:
    """Return the shared top-level folder for a browser folder upload, if any."""
    roots = set()
    saw_nested_path = False
    for raw_path in relative_paths:
        path = (raw_path or "").strip().lstrip("/\\")
        if not path:
            continue
        parts = [part for part in re.split(r"[\\/]+", path) if part]
        if len(parts) < 2:
            return None
        saw_nested_path = True
        roots.add(parts[0])
        if len(roots) > 1:
            return None
    if not saw_nested_path or not roots:
        return None
    return next(iter(roots))


def _browser_upload_source_name(value: str) -> Optional[str]:
    cleaned = re.sub(r"[\\/]+", " ", str(value or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:160] or None


def _safe_upload_relative_path(raw_path: str, fallback_name: str) -> str:
    candidate = str(raw_path or fallback_name or "upload").replace("\\", "/")
    candidate = candidate.replace("\x00", "").strip().lstrip("/")
    parts = []
    for part in re.split(r"/+", candidate):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("Upload paths may not contain '..' segments.")
        if re.search(r"[\x00-\x1f\x7f]", part):
            raise ValueError("Upload paths may not contain control characters.")
        parts.append(part)
    if not parts:
        parts = [_safe_download_name(fallback_name or "upload")]
    return "/".join(parts)


def _uploaded_file_size(uploaded_file) -> int:
    content_length = getattr(uploaded_file, "content_length", None)
    if content_length is not None:
        try:
            value = int(content_length)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    stream = getattr(uploaded_file, "stream", None)
    if stream is None:
        return 0
    try:
        current = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = int(stream.tell())
        stream.seek(current)
        return max(0, size)
    except Exception:
        return 0


def _normalize_virtual_path(path: str) -> str:
    return re.sub(r"[\\/]+", "/", str(path or "").strip()).strip("/")


def _safe_download_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", str(value or "").strip()).strip(" .")
    return cleaned or "download"


def _archives_page_size(raw_value: Optional[str] = None) -> int:
    configured = env_int("GITHUB_DRIVE_ARCHIVES_PAGE_SIZE", 24)
    configured = max(1, min(configured, 100))
    if raw_value is None or str(raw_value).strip() == "":
        return configured
    value = int(str(raw_value).strip())
    return max(1, min(value, 100))


def _storage_limit_bytes() -> Optional[int]:
    raw = (os.environ.get("GITHUB_DRIVE_STORAGE_LIMIT_BYTES") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# ── entry point ──────────────────────────────────────────────────────────────


def run_web(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    _configure_runtime_noise()
    port = int(os.environ.get("PORT", port))
    app = create_app()
    if open_browser:
        import webbrowser
        timer = threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}"))
        timer.daemon = True
        timer.start()
    app.run(host=host, port=port, debug=False, threaded=True)


def _configure_runtime_noise() -> None:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL 1.1.1.*")
