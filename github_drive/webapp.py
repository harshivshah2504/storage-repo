import base64
import functools
import io
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
from typing import Any, Callable, Dict, List, Optional

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import users
from .api import GitHubClient, parse_owner_repo
from .auth_manager import ensure_state_dir, restore_from_env, state_status
from .storage import (
    delete_archive,
    delete_archive_file,
    download_archive,
    list_archive_contents,
    list_remote_archives,
    read_archive_file,
    upload_archive,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB
LOG = logging.getLogger("github_drive.webapp")

_TASKS: Dict[str, Dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
_DOWNLOAD_DIRS: Dict[str, str] = {}
_DOWNLOAD_LOCK = threading.Lock()


def create_app() -> Flask:
    from werkzeug.middleware.proxy_fix import ProxyFix

    ensure_state_dir()
    restore_from_env()

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
        return render_template(
            "login.html",
            asset_version=_asset_version(),
            allow_signup=users.signup_enabled(),
            mode="login",
            error=None,
        )

    @app.post("/login")
    def login_submit():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = users.verify_password(username, password)
        if not user:
            return render_template(
                "login.html",
                asset_version=_asset_version(),
                allow_signup=users.signup_enabled(),
                mode="login",
                error="Invalid username or password.",
            ), 401
        session.clear()
        session["user_id"] = user["username"]
        session.permanent = True
        return redirect(url_for("index"))

    @app.get("/signup")
    def signup_page():
        if not users.signup_enabled():
            return _signup_disabled_response()
        return render_template(
            "login.html",
            asset_version=_asset_version(),
            allow_signup=True,
            mode="signup",
            error=None,
        )

    @app.post("/signup")
    def signup_submit():
        if not users.signup_enabled():
            return _signup_disabled_response()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if password != confirm:
            return render_template(
                "login.html",
                asset_version=_asset_version(),
                allow_signup=True,
                mode="signup",
                error="Passwords do not match.",
            ), 400
        try:
            user = users.create_user(username, password)
        except ValueError as exc:
            return render_template(
                "login.html",
                asset_version=_asset_version(),
                allow_signup=True,
                mode="signup",
                error=str(exc),
            ), 400
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
        return render_template("index.html", asset_version=_asset_version(), username=g.user_id)

    # ── user / GitHub credentials API ────────────────────────────────────────

    @app.get("/api/me")
    @login_required
    def api_me():
        status = users.get_user_status(g.user_id)
        return jsonify(status)

    @app.post("/api/me/credentials")
    @login_required
    def api_set_credentials():
        payload = request.get_json(force=True) or {}
        token = (payload.get("token") or "").strip()
        repo_slug = (payload.get("repo") or "").strip()
        if not token:
            return jsonify({"error": "token is required"}), 400
        if not repo_slug or "/" not in repo_slug:
            return jsonify({"error": "repo is required (owner/repo)"}), 400
        try:
            owner, repo = parse_owner_repo(repo_slug)
            client = GitHubClient(token=token, owner=owner, repo=repo)
            login = client.viewer_login()
            if payload.get("create_repo"):
                client.ensure_repo(private=bool(payload.get("private_repo", True)))
            users.set_user_credentials(g.user_id, token, repo_slug)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"login": login, "repo": f"{owner}/{repo}"})

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
            result = list_remote_archives(client=client)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"archives": result})

    @app.get("/api/archives/<int:release_id>/cover")
    @login_required
    def archive_cover(release_id: int):
        from . import thumbnails
        try:
            client = _user_client(g.user_id)
            assets = client.list_release_assets(release_id)
        except Exception:
            abort(404)
        cover = next((a for a in assets if a["name"] == thumbnails.COVER_ASSET_NAME), None)
        if not cover:
            legacy_image = thumbnails.first_image_asset(assets)
            if not legacy_image:
                abort(404)
            try:
                original = client.download_asset_bytes(legacy_image["id"])
                data = thumbnails.make_cover_jpeg_from_bytes(original)
            except Exception:
                abort(404)
            if not data:
                abort(404)
            try:
                uploaded = client.upload_asset_bytes(
                    release_id=release_id,
                    asset_name=thumbnails.COVER_ASSET_NAME,
                    payload=data,
                    content_type="image/jpeg",
                )
                cover = uploaded
            except Exception:
                cover = None
            return Response(
                data,
                mimetype="image/jpeg",
                headers={"Cache-Control": "private, max-age=600"},
            )
        try:
            data = client.download_asset_bytes(cover["id"])
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
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(result)

    @app.get("/api/archives/<int:release_id>/file")
    @login_required
    def archive_file(release_id: int):
        from . import thumbnails

        relative_path = (request.args.get("path") or "").strip()
        if not relative_path:
            return jsonify({"error": "path is required"}), 400
        thumb = (request.args.get("thumb") or "").strip().lower() in {"1", "true", "yes"}
        try:
            client = _user_client(g.user_id)
            encode_key = _user_encode_key(g.user_id)
            payload, content_type = read_archive_file(
                release_id=release_id,
                relative_path=relative_path,
                encode_key=encode_key,
                client=client,
            )
            if thumb:
                thumb_bytes = thumbnails.make_cover_jpeg_from_bytes(payload)
                if not thumb_bytes:
                    abort(404)
                return Response(
                    thumb_bytes,
                    mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=600"},
                )
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            abort(404)
        return Response(
            payload,
            mimetype=content_type or "application/octet-stream",
            headers={"Cache-Control": "private, max-age=600"},
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
                payload, content_type = read_archive_file(
                    release_id=release_id,
                    relative_path=relative_path,
                    encode_key=encode_key,
                    client=client,
                )
                return send_file(
                    io.BytesIO(payload),
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
            if not matched_entries:
                return jsonify({"error": f"Folder {relative_path!r} was not found in this archive."}), 404

            folder_name = Path(relative_path).name or "folder"
            temp_dir = Path(tempfile.mkdtemp(prefix="github-drive-entry-zip-"))
            zip_path = temp_dir / f"{_safe_download_name(folder_name)}.zip"
            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for entry in matched_entries:
                    entry_path = entry.get("relative_path") or ""
                    payload, _content_type = read_archive_file(
                        release_id=release_id,
                        relative_path=entry_path,
                        encode_key=encode_key,
                        client=client,
                    )
                    remainder = entry_path[len(prefix):].lstrip("/")
                    if not remainder:
                        continue
                    archive.writestr(f"{folder_name}/{remainder}", payload)

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
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 400
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
        task_data = _get_task(task_id, g.user_id)
        if task_data is None:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task_data)

    # ── upload (per user) ────────────────────────────────────────────────────

    @app.post("/api/upload-files")
    @login_required
    def upload_files():
        creds = users.get_user_credentials(g.user_id)
        if not creds:
            return jsonify({"error": "Configure your GitHub token first."}), 400

        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            return jsonify({"error": "No files were uploaded"}), 400

        relative_paths = request.form.getlist("relative_paths")
        if relative_paths and len(relative_paths) != len(uploaded_files):
            return jsonify({"error": "relative_paths count does not match uploaded files"}), 400
        requested_source_name = _browser_upload_source_name(request.form.get("source_name_override") or "")
        requested_source_type = (request.form.get("source_type_override") or "").strip().lower()
        if requested_source_type not in {"file", "directory"}:
            requested_source_type = None

        staging_root = Path(tempfile.mkdtemp(prefix="github-drive-web-upload-"))
        saved_paths = []
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

        encrypt = request.form.get("encrypt", "false").lower() == "true"
        payload = {
            "source_path": source_path,
            "private_release": request.form.get("private_release", "false").lower() == "true",
            "workers": int(request.form.get("workers", 4)),
            "recursive": request.form.get("recursive", "true").lower() == "true",
            "retries": int(request.form.get("retries", 3)),
            "encrypt": encrypt,
            "upload_mode": (request.form.get("upload_mode") or "auto").strip().lower() or "auto",
            "resume_tag": (request.form.get("resume_tag") or "").strip() or None,
            "cleanup_staging_root": str(staging_root),
            "upload_origin": "browser-transfer",
            "uploaded_file_count": len(saved_paths),
        }
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
        creds = users.get_user_credentials(g.user_id)
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
                "workers": int(payload.get("workers", 4)),
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
    encrypt = bool(payload.get("encrypt", False))
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
    _update_task(task_id, status="running")
    try:
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
    try:
        client = _user_client(user_id)
    except Exception as exc:
        _update_task(task_id, status="failed", error=str(exc))
        with _DOWNLOAD_LOCK:
            temp_dir = _DOWNLOAD_DIRS.pop(task_id, None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return
    _update_task(task_id, status="running")
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


def _create_task(task_type: str, user_id: str, payload: Dict[str, Any]) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _TASK_LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "type": task_type,
            "user_id": user_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "payload": payload,
            "logs": [],
            "result": None,
            "error": None,
            "progress_total": 0,
            "progress_done": 0,
        }
    return task_id


def _start_task_thread(task_id: str, runner) -> None:
    thread = threading.Thread(target=runner, args=(task_id,), daemon=True)
    thread.start()


def _get_task_internal(task_id: str) -> Optional[Dict]:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        return _serialize(task) if task else None


def _get_task(task_id: str, user_id: str) -> Optional[Dict]:
    task = _get_task_internal(task_id)
    if task is None:
        return None
    if task.get("user_id") != user_id:
        return None
    return task


def _list_tasks(user_id: str) -> List[Dict]:
    with _TASK_LOCK:
        tasks = [t for t in _TASKS.values() if t.get("user_id") == user_id]
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return [_serialize(t) for t in tasks]


def _update_task(task_id: str, **updates) -> None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task.update(updates)
        task["updated_at"] = time.time()


def _task_progress(task_id: str, event: str, payload: Dict[str, Any]) -> None:
    message = _format_progress_message(event, payload)
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["updated_at"] = time.time()
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
        task["logs"].append({
            "timestamp": time.time(),
            "event": event,
            "message": message,
            "payload": payload,
        })
        task["logs"] = task["logs"][-200:]


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
    return render_template(
        "login.html",
        asset_version=_asset_version(),
        allow_signup=False,
        mode="login",
        error="Signup is disabled on this server. Contact the administrator to create an account.",
    ), 403


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
        if request.path == "/healthz":
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


def _normalize_virtual_path(path: str) -> str:
    return re.sub(r"[\\/]+", "/", str(path or "").strip()).strip("/")


def _safe_download_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", str(value or "").strip()).strip(" .")
    return cleaned or "download"


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
