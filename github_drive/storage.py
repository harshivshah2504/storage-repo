import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import crypto
from .api import (
    GitHubClient,
    GitHubError,
    MANIFEST_ASSET_NAME,
    METADATA_VERSION,
    STORAGE_FORMAT,
    archive_tag_for,
    decode_archive_body,
    encode_archive_body,
    list_drive_archives,
    now_utc_iso,
)
from .auth_manager import get_client

ProgressCallback = Optional[Callable[[str, Dict], None]]
ENCRYPTED_SUFFIX = ".enc"
DEFAULT_CHUNK_BYTES = 1_900_000_000  # ~1.9 GB; sits comfortably under GitHub's 2 GB asset cap.
COPY_BUFFER = 4 * 1024 * 1024
UPLOAD_MODE_AUTO = "auto"
UPLOAD_MODE_FILES = "files"
UPLOAD_MODE_BUNDLE = "bundle"
STORAGE_MODE_FILE_ASSETS = "file-assets"
STORAGE_MODE_BUNDLE_ASSETS = "bundle-assets"
BUNDLE_ARCHIVE_SUFFIX = ".bundle.zip"
BUNDLE_FILE_COUNT_THRESHOLD = 256
BUNDLE_TINY_FILE_THRESHOLD = 8 * 1024 * 1024


def _chunk_size_bytes() -> int:
    raw = os.environ.get("GITHUB_DRIVE_CHUNK_BYTES")
    if not raw:
        return DEFAULT_CHUNK_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CHUNK_BYTES
    return value if value > 0 else DEFAULT_CHUNK_BYTES


def _split_threshold(encrypt: bool) -> int:
    """Maximum bytes of source data per chunk before splitting kicks in.
    Subtract the GDRV header overhead when encrypting so the on-the-wire size
    of each chunk still fits within the configured chunk budget."""
    base = _chunk_size_bytes()
    overhead = crypto.HEADER_LEN if encrypt else 0
    return max(1, base - overhead)


@dataclass
class ArchiveItem:
    order: int
    asset_name: str
    asset_id: int
    relative_path: str
    original_size: int
    source_sha256: str
    encrypted: bool
    content_type: str
    parts: List[Dict] = field(default_factory=list)
    members: List[Dict] = field(default_factory=list)


@dataclass
class ArchiveManifest:
    archive_id: str
    release_id: int
    tag: str
    name: str
    html_url: str
    source_path: str
    created_at: str
    total_items: int
    encrypted: bool
    items: List[ArchiveItem]
    storage_mode: str = STORAGE_MODE_FILE_ASSETS


def emit_progress(callback: ProgressCallback, event: str, payload: Dict) -> None:
    if callback is not None:
        callback(event, payload)


def collect_file_entries(source_path: str, recursive: bool = True) -> List[Dict]:
    """Walk source_path and return a list of {source_path, relative_path, size_bytes}."""
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"{source} does not exist")

    if source.is_file():
        return [
            {
                "source_path": str(source),
                "relative_path": source.name,
                "size_bytes": source.stat().st_size,
            }
        ]

    pattern = "**/*" if recursive else "*"
    entries: List[Dict] = []
    for candidate in sorted(source.glob(pattern)):
        if not candidate.is_file():
            continue
        entries.append(
            {
                "source_path": str(candidate),
                "relative_path": str(candidate.relative_to(source)),
                "size_bytes": candidate.stat().st_size,
            }
        )
    if not entries:
        raise ValueError(f"No files were found in {source}")
    return entries


def list_remote_archives(client: Optional[GitHubClient] = None) -> List[Dict]:
    return list_drive_archives(client or get_client())


def list_archive_contents(
    release_id: Optional[int] = None,
    tag: Optional[str] = None,
    archive_id: Optional[str] = None,
    client: Optional[GitHubClient] = None,
) -> Dict:
    client = client or get_client()
    release, archive_meta, _assets, _by_name, _manifest, items, _encrypted, storage_mode = _load_archive_snapshot(
        client=client,
        release_id=release_id,
        tag=tag,
        archive_id=archive_id,
    )
    entries = _flatten_archive_entries(items, storage_mode)
    return {
        "release_id": release["id"],
        "tag": release.get("tag_name", ""),
        "name": release.get("name") or release.get("tag_name") or "",
        "html_url": release.get("html_url"),
        "created_at": release.get("created_at", ""),
        "updated_at": release.get("updated_at", ""),
        "archive": archive_meta,
        "storage_mode": storage_mode,
        "supports_file_delete": storage_mode == STORAGE_MODE_FILE_ASSETS,
        "entries": entries,
    }


def read_archive_file(
    relative_path: str,
    release_id: Optional[int] = None,
    tag: Optional[str] = None,
    archive_id: Optional[str] = None,
    encode_key: Optional[bytes] = None,
    client: Optional[GitHubClient] = None,
) -> Tuple[bytes, str]:
    client = client or get_client()
    _release, _archive_meta, _assets, _by_name, _manifest, items, encrypted, storage_mode = _load_archive_snapshot(
        client=client,
        release_id=release_id,
        tag=tag,
        archive_id=archive_id,
    )
    item, member = _find_archive_entry(items, storage_mode, relative_path)
    if item is None:
        raise RuntimeError(f"File {relative_path!r} was not found in this archive.")
    return _read_archive_entry_bytes(
        client=client,
        item=item,
        relative_path=relative_path,
        member=member,
        encrypted=encrypted,
        encode_key=encode_key,
    )


def delete_archive_file(
    relative_path: str,
    release_id: Optional[int] = None,
    tag: Optional[str] = None,
    archive_id: Optional[str] = None,
    encode_key: Optional[bytes] = None,
    client: Optional[GitHubClient] = None,
) -> Dict:
    client = client or get_client()
    release, archive_meta, _assets, by_name, manifest, items, encrypted, storage_mode = _load_archive_snapshot(
        client=client,
        release_id=release_id,
        tag=tag,
        archive_id=archive_id,
    )
    if storage_mode != STORAGE_MODE_FILE_ASSETS:
        raise RuntimeError("Individual file delete is unavailable for bundled archives.")

    target_item, _member = _find_archive_entry(items, storage_mode, relative_path)
    if target_item is None:
        raise RuntimeError(f"File {relative_path!r} was not found in this archive.")

    remaining_items = [item for item in items if item.get("relative_path") != relative_path]
    if not remaining_items:
        result = delete_archive(release_id=release["id"], client=client)
        result["archive_deleted"] = True
        result["deleted_path"] = relative_path
        return result

    for part in target_item.get("parts") or []:
        asset_id = part.get("asset_id")
        if asset_id:
            client.delete_asset(int(asset_id))

    manifest_payload = {
        "storage_format": STORAGE_FORMAT,
        "metadata_version": METADATA_VERSION,
        "archive_id": archive_meta.get("archive_id"),
        "source_name": archive_meta.get("source_name"),
        "source_path": archive_meta.get("source_path"),
        "created_at": archive_meta.get("created_at"),
        "total_items": len(remaining_items),
        "encrypted": bool(encrypted),
        "storage_mode": storage_mode,
        "items": [_manifest_item_from_download_item(item) for item in remaining_items],
    }

    remaining_paths = [item.get("relative_path") or "" for item in remaining_items]
    archive_meta["total_items"] = len(remaining_items)
    archive_meta["kinds"] = _classify_relative_paths(remaining_paths)
    archive_meta["cover_asset_name"] = COVER_ASSET_NAME if any(
        _is_image_path(path) for path in remaining_paths
    ) else None

    client.update_release(
        release["id"],
        name=_make_archive_title(archive_meta.get("source_name") or release.get("name") or "archive", len(remaining_items)),
        body=encode_archive_body(archive_meta),
    )

    existing_manifest = by_name.get(MANIFEST_ASSET_NAME)
    if existing_manifest:
        client.delete_asset(existing_manifest["id"])
    client.upload_asset_bytes(
        release_id=release["id"],
        asset_name=MANIFEST_ASSET_NAME,
        payload=json.dumps(manifest_payload, indent=2).encode("utf-8"),
        content_type="application/json",
    )

    existing_cover = by_name.get(COVER_ASSET_NAME)
    if existing_cover:
        try:
            client.delete_asset(existing_cover["id"])
        except Exception:
            pass
    next_image = next((item for item in remaining_items if _is_image_path(item.get("relative_path") or "")), None)
    if next_image:
        try:
            image_bytes, _content_type = _read_archive_entry_bytes(
                client=client,
                item=next_image,
                relative_path=next_image["relative_path"],
                member=None,
                encrypted=encrypted,
                encode_key=encode_key,
            )
            from . import thumbnails
            cover_bytes = thumbnails.make_cover_jpeg_from_bytes(image_bytes)
            if cover_bytes:
                client.upload_asset_bytes(
                    release_id=release["id"],
                    asset_name=COVER_ASSET_NAME,
                    payload=cover_bytes,
                    content_type="image/jpeg",
                )
        except Exception:
            pass

    return {
        "release_id": release["id"],
        "tag": release.get("tag_name", ""),
        "archive_deleted": False,
        "deleted_path": relative_path,
        "remaining_items": len(remaining_items),
        "archive": archive_meta,
    }


def upload_archive(
    source_path: str,
    private_release: bool = False,
    workers: int = 4,
    recursive: bool = True,
    retries: int = 3,
    encrypt: bool = False,
    encode_key: Optional[bytes] = None,
    upload_mode: str = UPLOAD_MODE_AUTO,
    resume_release_id: Optional[int] = None,
    resume_tag: Optional[str] = None,
    resume_archive_id: Optional[str] = None,
    progress: ProgressCallback = None,
    client: Optional[GitHubClient] = None,
) -> ArchiveManifest:
    if encrypt and not encode_key:
        raise RuntimeError("encrypt=True requires encode_key.")
    if encrypt:
        crypto._validate_key(encode_key)

    client = client or get_client()
    entries = collect_file_entries(source_path, recursive=recursive)
    source = Path(source_path).expanduser().resolve()
    upload_mode = _normalize_upload_mode(upload_mode)
    storage_mode = _choose_storage_mode(entries, upload_mode)

    from . import thumbnails
    kinds = thumbnails.classify_entries(entries)
    cover_candidate = thumbnails.first_image_entry(entries)

    created_at = now_utc_iso()
    archive_meta = {
        "storage_format": STORAGE_FORMAT,
        "metadata_version": METADATA_VERSION,
        "created_at": created_at,
        "source_name": source.name,
        "source_path": str(source),
        "total_items": len(entries),
        "encrypted": bool(encrypt),
        "storage_mode": storage_mode,
        "kinds": kinds,
        "cover_asset_name": thumbnails.COVER_ASSET_NAME if cover_candidate else None,
    }
    release, archive_meta = _prepare_upload_release(
        client=client,
        archive_meta=archive_meta,
        source_name=source.name,
        retries=retries,
        private_release=private_release,
        resume_release_id=resume_release_id,
        resume_tag=resume_tag,
        resume_archive_id=resume_archive_id,
    )
    created_at = archive_meta["created_at"]
    archive_id = archive_meta["archive_id"]
    tag = release.get("tag_name") or archive_tag_for(archive_id)
    title = release.get("name") or _make_archive_title(source.name, len(entries))
    release_id = release["id"]

    emit_progress(
        progress,
        "archive_created",
        {
            "archive_id": archive_id,
            "release_id": release_id,
            "tag": tag,
            "title": title,
            "total_items": len(entries),
            "html_url": release.get("html_url", ""),
        },
    )

    existing_assets = {asset["name"]: asset for asset in client.list_release_assets(release_id)}

    # Best-effort cover thumbnail. Failures are non-fatal — listing without _cover.jpg
    # falls back to the generic icon on the frontend.
    if cover_candidate and thumbnails.COVER_ASSET_NAME not in existing_assets:
        cover_bytes = thumbnails.make_cover_jpeg(cover_candidate["source_path"])
        if cover_bytes:
            try:
                client.upload_asset_bytes(
                    release_id=release_id,
                    asset_name=thumbnails.COVER_ASSET_NAME,
                    payload=cover_bytes,
                    content_type="image/jpeg",
                )
            except Exception:
                pass

    items: List[ArchiveItem] = []
    completed_items = 0

    if storage_mode == STORAGE_MODE_BUNDLE_ASSETS:
        bundle_item = _upload_bundle_archive(
            entries=entries,
            source=source,
            encrypt=encrypt,
            encode_key=encode_key,
            client=client,
            release_id=release_id,
            retries=retries,
            progress=progress,
            existing_assets=existing_assets,
        )
        if bundle_item:
            items.append(bundle_item)
            if bundle_item.source_sha256:
                completed_items = 0
            else:
                completed_items = len(bundle_item.members)
    else:
        pending: List[Dict] = []
        for index, entry in enumerate(entries):
            plan = _plan_entry_assets(index, entry, encrypt)
            if all(part["asset_name"] in existing_assets for part in plan):
                parts_meta = [
                    {
                        "order": part["chunk_index"],
                        "asset_name": part["asset_name"],
                        "asset_id": existing_assets[part["asset_name"]]["id"],
                        "size": int(existing_assets[part["asset_name"]].get("size") or 0),
                    }
                    for part in plan
                ]
                first = parts_meta[0]
                items.append(
                    ArchiveItem(
                        order=index,
                        asset_name=first["asset_name"],
                        asset_id=first["asset_id"],
                        relative_path=entry["relative_path"],
                        original_size=int(entry["size_bytes"]),
                        source_sha256="",
                        encrypted=bool(encrypt),
                        content_type=existing_assets[first["asset_name"]].get("content_type", "application/octet-stream"),
                        parts=parts_meta,
                    )
                )
                emit_progress(
                    progress,
                    "item_skipped",
                    {
                        "order": index,
                        "relative_path": entry["relative_path"],
                        "asset_name": first["asset_name"],
                        "parts": len(parts_meta),
                        "progress_increment": 1,
                    },
                )
                completed_items += 1
                continue
            pending.append({"index": index, "entry": entry, "plan": plan})

        max_workers = max(1, min(int(workers), len(pending) or 1))
        if pending:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _upload_entry,
                        client=client,
                        release_id=release_id,
                        order=task["index"],
                        plan=task["plan"],
                        entry=task["entry"],
                        encrypt=encrypt,
                        encode_key=encode_key,
                        retries=retries,
                        progress=progress,
                        existing_assets=existing_assets,
                    ): task
                    for task in pending
                }
                for future in as_completed(futures):
                    items.append(future.result())

    if completed_items:
        emit_progress(
            progress,
            "archive_resumed",
            {
                "archive_id": archive_id,
                "release_id": release_id,
                "completed_items": completed_items,
                "total_items": len(entries),
            },
        )

    items.sort(key=lambda item: item.order)

    manifest_payload = {
        "storage_format": STORAGE_FORMAT,
        "metadata_version": METADATA_VERSION,
        "archive_id": archive_id,
        "created_at": created_at,
        "source_name": source.name,
        "source_path": str(source),
        "total_items": len(entries),
        "encrypted": bool(encrypt),
        "storage_mode": storage_mode,
        "items": [asdict(item) for item in items],
    }
    if MANIFEST_ASSET_NAME in existing_assets:
        _retry(
            "delete stale manifest",
            retries,
            lambda: client.delete_asset(existing_assets[MANIFEST_ASSET_NAME]["id"]),
        )
    _retry(
        "upload manifest",
        retries,
        lambda: client.upload_asset_bytes(
            release_id=release_id,
            asset_name=MANIFEST_ASSET_NAME,
            payload=json.dumps(manifest_payload, indent=2).encode("utf-8"),
            content_type="application/json",
        ),
    )

    manifest = ArchiveManifest(
        archive_id=archive_id,
        release_id=release_id,
        tag=tag,
        name=title,
        html_url=release.get("html_url", ""),
        source_path=str(source),
        created_at=created_at,
        total_items=len(entries),
        encrypted=bool(encrypt),
        storage_mode=storage_mode,
        items=items,
    )
    emit_progress(
        progress,
        "archive_uploaded",
        {
            "archive_id": archive_id,
            "release_id": release_id,
            "tag": tag,
            "html_url": release.get("html_url", ""),
            "total_items": len(entries),
        },
    )
    return manifest


def _upload_entry(
    client: GitHubClient,
    release_id: int,
    order: int,
    plan: List[Dict],
    entry: Dict,
    encrypt: bool,
    encode_key: Optional[bytes],
    retries: int,
    progress: ProgressCallback,
    existing_assets: Dict[str, Dict],
    members: Optional[List[Dict]] = None,
) -> ArchiveItem:
    """Upload a single source file to the release.

    `plan` describes the chunks the file will be split into. For files at or below the
    chunk threshold this is exactly one entry, and the on-the-wire format matches what
    earlier (single-asset) archives produced. Larger files are split into multiple chunks;
    each chunk is uploaded as its own asset and is independently encrypted when encryption
    is enabled, so any single chunk fits in RAM regardless of total file size.
    """
    relative_path = entry["relative_path"]
    source_path = entry["source_path"]
    multipart = len(plan) > 1

    emit_progress(
        progress,
        "item_preparing",
        {
            "order": order,
            "relative_path": relative_path,
            "parts": len(plan),
            "multipart": multipart,
        },
    )

    if encrypt:
        content_type = "application/octet-stream"
    else:
        guessed, _ = mimetypes.guess_type(source_path)
        content_type = guessed or "application/octet-stream"

    work_dir = tempfile.mkdtemp(prefix="github-drive-upload-")
    try:
        sha = _sha256_file(source_path)
        parts_meta: List[Dict] = []
        with open(source_path, "rb") as src:
            for part in plan:
                chunk_index = part["chunk_index"]
                asset_name = part["asset_name"]

                if asset_name in existing_assets:
                    asset = existing_assets[asset_name]
                    parts_meta.append({
                        "order": chunk_index,
                        "asset_name": asset_name,
                        "asset_id": asset["id"],
                        "size": int(asset.get("size") or 0),
                    })
                    src.seek(part["chunk_offset"] + part["chunk_length"])
                    continue

                # Materialise this chunk on disk (raw bytes, then optionally encrypt).
                if multipart:
                    raw_path = os.path.join(work_dir, f"chunk-{chunk_index:04d}.bin")
                    _write_range(src, raw_path, part["chunk_length"])
                else:
                    raw_path = source_path
                upload_path = raw_path
                if encrypt:
                    upload_path = os.path.join(work_dir, f"chunk-{chunk_index:04d}.enc")
                    crypto.encrypt_file(raw_path, upload_path, encode_key)
                    if multipart and raw_path != source_path:
                        os.unlink(raw_path)

                asset = _retry(
                    f"upload {relative_path} part {chunk_index + 1}/{len(plan)}",
                    retries,
                    lambda path=upload_path, name=asset_name: client.upload_asset(
                        release_id=release_id,
                        asset_name=name,
                        file_path=path,
                        content_type=content_type,
                    ),
                )
                parts_meta.append({
                    "order": chunk_index,
                    "asset_name": asset_name,
                    "asset_id": asset["id"],
                    "size": int(asset.get("size") or os.path.getsize(upload_path)),
                })

                if multipart and upload_path != source_path:
                    try:
                        os.unlink(upload_path)
                    except OSError:
                        pass

        first = parts_meta[0]
        emit_progress(
            progress,
            "item_uploaded",
            {
                "order": order,
                "relative_path": relative_path,
                "asset_name": first["asset_name"],
                "asset_id": first["asset_id"],
                "encrypted": bool(encrypt),
                "parts": len(parts_meta),
                "multipart": multipart,
                "progress_increment": len(members or []) or 1,
            },
        )
        return ArchiveItem(
            order=order,
            asset_name=first["asset_name"],
            asset_id=first["asset_id"],
            relative_path=relative_path,
            original_size=int(entry["size_bytes"]),
            source_sha256=sha,
            encrypted=bool(encrypt),
            content_type=content_type,
            parts=parts_meta,
            members=list(members or []),
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _upload_bundle_archive(
    entries: List[Dict],
    source: Path,
    encrypt: bool,
    encode_key: Optional[bytes],
    client: GitHubClient,
    release_id: int,
    retries: int,
    progress: ProgressCallback,
    existing_assets: Dict[str, Dict],
) -> ArchiveItem:
    work_dir = tempfile.mkdtemp(prefix="github-drive-bundle-")
    try:
        bundle_entry, members = _create_bundle_archive(entries, source, work_dir)
        plan = _plan_entry_assets(0, bundle_entry, encrypt)
        if all(part["asset_name"] in existing_assets for part in plan):
            parts_meta = [
                {
                    "order": part["chunk_index"],
                    "asset_name": part["asset_name"],
                    "asset_id": existing_assets[part["asset_name"]]["id"],
                    "size": int(existing_assets[part["asset_name"]].get("size") or 0),
                }
                for part in plan
            ]
            first = parts_meta[0]
            emit_progress(
                progress,
                "item_skipped",
                {
                    "order": 0,
                    "relative_path": bundle_entry["relative_path"],
                    "asset_name": first["asset_name"],
                    "parts": len(parts_meta),
                    "progress_increment": len(members),
                },
            )
            return ArchiveItem(
                order=0,
                asset_name=first["asset_name"],
                asset_id=first["asset_id"],
                relative_path=bundle_entry["relative_path"],
                original_size=int(bundle_entry["size_bytes"]),
                source_sha256="",
                encrypted=bool(encrypt),
                content_type=existing_assets[first["asset_name"]].get("content_type", "application/octet-stream"),
                parts=parts_meta,
                members=members,
            )

        return _upload_entry(
            client=client,
            release_id=release_id,
            order=0,
            plan=plan,
            entry=bundle_entry,
            encrypt=encrypt,
            encode_key=encode_key,
            retries=retries,
            progress=progress,
            existing_assets=existing_assets,
            members=members,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _create_bundle_archive(entries: List[Dict], source: Path, work_dir: str) -> Tuple[Dict, List[Dict]]:
    bundle_name = _bundle_relative_name(source.name)
    bundle_path = os.path.join(work_dir, bundle_name)
    members: List[Dict] = []

    with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for entry in entries:
            relative_path = entry["relative_path"]
            source_path = entry["source_path"]
            archive.write(source_path, arcname=relative_path)
            guessed, _ = mimetypes.guess_type(source_path)
            members.append(
                {
                    "relative_path": relative_path,
                    "original_size": int(entry["size_bytes"]),
                    "source_sha256": _sha256_file(source_path),
                    "content_type": guessed or "application/octet-stream",
                }
            )

    bundle_entry = {
        "source_path": bundle_path,
        "relative_path": bundle_name,
        "size_bytes": os.path.getsize(bundle_path),
    }
    return bundle_entry, members


def _normalize_upload_mode(mode: str) -> str:
    raw = (mode or UPLOAD_MODE_AUTO).strip().lower()
    if raw not in {UPLOAD_MODE_AUTO, UPLOAD_MODE_FILES, UPLOAD_MODE_BUNDLE}:
        raise RuntimeError(f"Unsupported upload mode {mode!r}. Expected one of: auto, files, bundle.")
    return raw


def _choose_storage_mode(entries: List[Dict], upload_mode: str) -> str:
    if upload_mode == UPLOAD_MODE_FILES:
        return STORAGE_MODE_FILE_ASSETS
    if upload_mode == UPLOAD_MODE_BUNDLE:
        return STORAGE_MODE_BUNDLE_ASSETS
    return STORAGE_MODE_BUNDLE_ASSETS if _should_bundle_entries(entries) else STORAGE_MODE_FILE_ASSETS


def _should_bundle_entries(entries: List[Dict]) -> bool:
    if len(entries) <= 1:
        return False
    if len(entries) >= 1000:
        return True
    total_bytes = sum(int(entry["size_bytes"]) for entry in entries)
    average = total_bytes / max(len(entries), 1)
    return len(entries) >= BUNDLE_FILE_COUNT_THRESHOLD and average <= BUNDLE_TINY_FILE_THRESHOLD


def _prepare_upload_release(
    client: GitHubClient,
    archive_meta: Dict,
    source_name: str,
    retries: int,
    private_release: bool,
    resume_release_id: Optional[int],
    resume_tag: Optional[str],
    resume_archive_id: Optional[str],
) -> Tuple[Dict, Dict]:
    if any(value is not None and value != "" for value in (resume_release_id, resume_tag, resume_archive_id)):
        release = _resolve_release(
            client,
            release_id=resume_release_id,
            tag=resume_tag,
            archive_id=resume_archive_id,
        )
        resume_meta = decode_archive_body(release.get("body") or "")
        if not resume_meta:
            raise RuntimeError(f"Release {release.get('tag_name')} is not a github-drive archive.")
        if (resume_meta.get("source_name") or "") != source_name:
            raise RuntimeError(
                f"Resume target {release.get('tag_name')} belongs to {resume_meta.get('source_name')!r}, "
                f"not {source_name!r}."
            )
        if bool(resume_meta.get("encrypted")) != bool(archive_meta.get("encrypted")):
            raise RuntimeError("Resume target encryption setting does not match this upload.")
        if (resume_meta.get("storage_mode") or STORAGE_MODE_FILE_ASSETS) != archive_meta["storage_mode"]:
            raise RuntimeError("Resume target storage mode does not match this upload mode.")
        archive_meta["archive_id"] = resume_meta.get("archive_id") or uuid.uuid4().hex[:12].upper()
        archive_meta["created_at"] = resume_meta.get("created_at") or archive_meta["created_at"]
        return release, archive_meta

    archive_id = uuid.uuid4().hex[:12].upper()
    archive_meta["archive_id"] = archive_id
    title = _make_archive_title(source_name, int(archive_meta["total_items"]))
    body = encode_archive_body(archive_meta)
    release = _retry(
        "create release",
        retries,
        lambda: client.create_release(
            tag=archive_tag_for(archive_id),
            name=title,
            body=body,
            draft=False,
            prerelease=bool(private_release),
        ),
    )
    return release, archive_meta


def _plan_entry_assets(order: int, entry: Dict, encrypt: bool) -> List[Dict]:
    """Decide how many chunks an entry needs and the asset name + offset for each."""
    file_size = int(entry["size_bytes"])
    relative_path = entry["relative_path"]
    threshold = _split_threshold(encrypt)

    if file_size <= threshold:
        return [{
            "chunk_index": 0,
            "chunk_offset": 0,
            "chunk_length": file_size,
            "asset_name": _asset_name_for(order, relative_path, encrypt),
        }]

    plan: List[Dict] = []
    offset = 0
    chunk_index = 0
    remaining = file_size if file_size > 0 else 0
    while remaining > 0:
        length = min(threshold, remaining)
        plan.append({
            "chunk_index": chunk_index,
            "chunk_offset": offset,
            "chunk_length": length,
            "asset_name": _part_asset_name_for(order, relative_path, chunk_index, encrypt),
        })
        offset += length
        remaining -= length
        chunk_index += 1
    return plan


def _write_range(src, output_path: str, length: int) -> None:
    """Stream `length` bytes from the open `src` file to `output_path`."""
    with open(output_path, "wb") as dst:
        remaining = length
        while remaining > 0:
            buf = src.read(min(COPY_BUFFER, remaining))
            if not buf:
                break
            dst.write(buf)
            remaining -= len(buf)


def download_archive(
    release_id: Optional[int] = None,
    tag: Optional[str] = None,
    archive_id: Optional[str] = None,
    destination_dir: str = "",
    workers: int = 4,
    skip_existing: bool = True,
    retries: int = 3,
    encode_key: Optional[bytes] = None,
    progress: ProgressCallback = None,
    client: Optional[GitHubClient] = None,
) -> Dict:
    if not destination_dir:
        raise RuntimeError("destination_dir is required.")
    client = client or get_client()
    release = _resolve_release(client, release_id=release_id, tag=tag, archive_id=archive_id)
    archive_meta = decode_archive_body(release.get("body") or "")
    if not archive_meta:
        raise RuntimeError(f"Release {release.get('tag_name')} is not a github-drive archive.")

    assets = client.list_release_assets(release["id"])
    items, encrypted, storage_mode, progress_total = _build_download_items(client, release["id"], assets, archive_meta)
    if encrypted and not encode_key:
        raise RuntimeError("This archive is encrypted; an encode_key is required to download.")
    if encrypted:
        crypto._validate_key(encode_key)

    destination = Path(destination_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    emit_progress(
        progress,
        "archive_downloading",
        {
            "release_id": release["id"],
            "tag": release.get("tag_name", ""),
            "title": release.get("name", ""),
            "total_items": progress_total,
            "destination_dir": str(destination),
        },
    )

    max_workers = max(1, min(int(workers), len(items) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _download_item,
                client=client,
                item=item,
                destination=str(destination),
                encrypted=encrypted,
                encode_key=encode_key,
                skip_existing=skip_existing,
                retries=retries,
                progress=progress,
                storage_mode=storage_mode,
            ): item
            for item in items
        }
        for future in as_completed(futures):
            future.result()

    result = {
        "release_id": release["id"],
        "tag": release.get("tag_name", ""),
        "title": release.get("name", ""),
        "destination_dir": str(destination),
        "archive": archive_meta,
        "downloaded_items": progress_total,
    }
    emit_progress(progress, "archive_downloaded", result)
    return result


def _resolve_release(
    client: GitHubClient,
    release_id: Optional[int],
    tag: Optional[str],
    archive_id: Optional[str],
) -> Dict:
    if release_id:
        return client.get_release(int(release_id))
    if tag:
        release = client.get_release_by_tag(tag)
        if not release:
            raise RuntimeError(f"Release with tag {tag} not found.")
        return release
    if archive_id:
        release = client.get_release_by_tag(archive_tag_for(archive_id))
        if not release:
            raise RuntimeError(f"Archive with id {archive_id} not found.")
        return release
    raise RuntimeError("One of release_id, tag, or archive_id must be provided.")


def _build_download_items(
    client: GitHubClient,
    release_id: int,
    assets: List[Dict],
    archive_meta: Dict,
) -> Tuple[List[Dict], bool, str, int]:
    by_name = {asset["name"]: asset for asset in assets}
    manifest = None
    if MANIFEST_ASSET_NAME in by_name:
        try:
            manifest_bytes = client.download_asset_bytes(by_name[MANIFEST_ASSET_NAME]["id"])
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (GitHubError, json.JSONDecodeError, UnicodeDecodeError):
            manifest = None

    selected_meta = manifest or archive_meta
    encrypted = bool(selected_meta.get("encrypted"))
    storage_mode = selected_meta.get("storage_mode") or STORAGE_MODE_FILE_ASSETS
    items: List[Dict] = []
    if manifest and isinstance(manifest.get("items"), list):
        for entry in manifest["items"]:
            parts_meta = entry.get("parts") or []
            parts: List[Dict] = []
            for part in parts_meta:
                asset = by_name.get(part.get("asset_name"))
                if not asset:
                    continue
                parts.append({
                    "order": int(part.get("order", 0)),
                    "asset_id": asset["id"],
                    "asset_name": asset["name"],
                    "size": int(asset.get("size", part.get("size") or 0)),
                })
            if not parts:
                # Legacy manifest without `parts`: synthesise a single-part record from
                # the top-level asset_name on the manifest entry.
                asset_name = entry.get("asset_name")
                asset = by_name.get(asset_name) if asset_name else None
                if not asset:
                    continue
                parts.append({
                    "order": 0,
                    "asset_id": asset["id"],
                    "asset_name": asset["name"],
                    "size": int(asset.get("size", 0)),
                })
            parts.sort(key=lambda part: part["order"])
            first = parts[0]
            items.append(
                {
                    "order": int(entry.get("order", 0)),
                    "asset_id": first["asset_id"],
                    "asset_name": first["asset_name"],
                    "relative_path": entry.get("relative_path") or first["asset_name"],
                    "encrypted": bool(entry.get("encrypted", encrypted)),
                    "original_size": int(entry.get("original_size", first["size"] or 0)),
                    "content_type": entry.get("content_type") or asset.get("content_type", "application/octet-stream"),
                    "source_sha256": entry.get("source_sha256") or "",
                    "parts": parts,
                    "members": list(entry.get("members") or []),
                }
            )
    else:
        for asset in assets:
            if asset["name"] == MANIFEST_ASSET_NAME:
                continue
            order, relative_path, asset_encrypted = _decode_asset_name(asset["name"], encrypted)
            items.append(
                {
                    "order": order,
                    "asset_id": asset["id"],
                    "asset_name": asset["name"],
                    "relative_path": relative_path,
                    "encrypted": asset_encrypted,
                    "original_size": int(asset.get("size", 0)),
                    "content_type": asset.get("content_type", "application/octet-stream"),
                    "source_sha256": "",
                    "parts": [{
                        "order": 0,
                        "asset_id": asset["id"],
                        "asset_name": asset["name"],
                        "size": int(asset.get("size", 0)),
                    }],
                    "members": [],
                }
            )
    items.sort(key=lambda entry: entry["order"])
    if storage_mode == STORAGE_MODE_BUNDLE_ASSETS:
        progress_total = sum(len(item.get("members") or []) for item in items)
    else:
        progress_total = len(items)
    return items, encrypted, storage_mode, progress_total


def _download_item(
    client: GitHubClient,
    item: Dict,
    destination: str,
    encrypted: bool,
    encode_key: Optional[bytes],
    skip_existing: bool,
    retries: int,
    progress: ProgressCallback,
    storage_mode: str,
) -> None:
    if storage_mode == STORAGE_MODE_BUNDLE_ASSETS:
        _download_bundle_item(
            client=client,
            item=item,
            destination=destination,
            encrypted=encrypted,
            encode_key=encode_key,
            skip_existing=skip_existing,
            retries=retries,
            progress=progress,
        )
        return

    target = _resolve_destination_path(destination, item["relative_path"])
    target.parent.mkdir(parents=True, exist_ok=True)

    if skip_existing and target.exists():
        emit_progress(
            progress,
            "item_skipped",
            {
                "order": item["order"],
                "relative_path": item["relative_path"],
                "output_path": str(target),
            },
        )
        return

    emit_progress(
        progress,
        "item_downloading",
        {
            "order": item["order"],
            "relative_path": item["relative_path"],
            "asset_id": item["asset_id"],
        },
    )

    parts = sorted(item.get("parts") or [], key=lambda part: part["order"])
    if not parts:
        parts = [{
            "order": 0,
            "asset_id": item["asset_id"],
            "asset_name": item["asset_name"],
            "size": item.get("original_size", 0),
        }]
    is_encrypted = bool(item.get("encrypted") or encrypted)

    temp_dir = tempfile.mkdtemp(prefix="github-drive-dl-")
    try:
        # Single-part path keeps the historical "download then move" shape so single-asset
        # archives behave exactly as they did before chunking landed.
        if len(parts) == 1:
            raw_path = os.path.join(temp_dir, "asset.bin")
            part = parts[0]
            _retry(
                f"download {item['relative_path']}",
                retries,
                lambda: client.download_asset(part["asset_id"], raw_path),
            )
            if is_encrypted:
                crypto.decrypt_file(raw_path, str(target), encode_key)
            else:
                shutil.move(raw_path, str(target))
        else:
            # Multi-part: stream each chunk to a tempfile, optionally decrypt, append to
            # the final target, then drop the chunk to keep peak disk usage to ~1 chunk.
            with open(target, "wb") as out:
                for index, part in enumerate(parts):
                    raw_path = os.path.join(temp_dir, f"part-{index:04d}.bin")
                    _retry(
                        f"download {item['relative_path']} part {index + 1}/{len(parts)}",
                        retries,
                        lambda pid=part["asset_id"], path=raw_path: client.download_asset(pid, path),
                    )
                    if is_encrypted:
                        decrypted_path = raw_path + ".dec"
                        crypto.decrypt_file(raw_path, decrypted_path, encode_key)
                        os.unlink(raw_path)
                        chunk_path = decrypted_path
                    else:
                        chunk_path = raw_path
                    with open(chunk_path, "rb") as src:
                        while True:
                            buf = src.read(COPY_BUFFER)
                            if not buf:
                                break
                            out.write(buf)
                    os.unlink(chunk_path)

        emit_progress(
            progress,
            "item_downloaded",
            {
                "order": item["order"],
                "relative_path": item["relative_path"],
                "output_path": str(target),
                "parts": len(parts),
            },
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _download_bundle_item(
    client: GitHubClient,
    item: Dict,
    destination: str,
    encrypted: bool,
    encode_key: Optional[bytes],
    skip_existing: bool,
    retries: int,
    progress: ProgressCallback,
) -> None:
    members = list(item.get("members") or [])
    if not members:
        return

    if skip_existing and all(_resolve_destination_path(destination, member["relative_path"]).exists() for member in members):
        for index, member in enumerate(members):
            target = _resolve_destination_path(destination, member["relative_path"])
            emit_progress(
                progress,
                "item_skipped",
                {
                    "order": index,
                    "relative_path": member["relative_path"],
                    "output_path": str(target),
                    "progress_increment": 1,
                },
            )
        return

    emit_progress(
        progress,
        "item_downloading",
        {
            "order": item["order"],
            "relative_path": item["relative_path"],
            "asset_id": item["asset_id"],
            "multipart": len(item.get("parts") or []) > 1,
        },
    )

    temp_dir = tempfile.mkdtemp(prefix="github-drive-bundle-dl-")
    bundle_path = os.path.join(temp_dir, "bundle.zip")
    try:
        _materialize_download_parts(
            client=client,
            item=item,
            output_path=bundle_path,
            encrypted=encrypted,
            encode_key=encode_key,
            retries=retries,
        )

        with zipfile.ZipFile(bundle_path, "r") as archive:
            info_by_name = {info.filename: info for info in archive.infolist()}
            for index, member in enumerate(members):
                relative = member["relative_path"]
                target = _resolve_destination_path(destination, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if skip_existing and target.exists():
                    emit_progress(
                        progress,
                        "item_skipped",
                        {
                            "order": index,
                            "relative_path": relative,
                            "output_path": str(target),
                            "progress_increment": 1,
                        },
                    )
                    continue
                info = info_by_name.get(relative)
                if info is None:
                    raise RuntimeError(f"Bundle archive is missing expected member {relative!r}.")
                with archive.open(info, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, COPY_BUFFER)
                emit_progress(
                    progress,
                    "item_downloaded",
                    {
                        "order": index,
                        "relative_path": relative,
                        "output_path": str(target),
                        "progress_increment": 1,
                    },
                )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _materialize_download_parts(
    client: GitHubClient,
    item: Dict,
    output_path: str,
    encrypted: bool,
    encode_key: Optional[bytes],
    retries: int,
) -> None:
    parts = sorted(item.get("parts") or [], key=lambda part: part["order"])
    if not parts:
        parts = [{
            "order": 0,
            "asset_id": item["asset_id"],
            "asset_name": item["asset_name"],
            "size": item.get("original_size", 0),
        }]
    is_encrypted = bool(item.get("encrypted") or encrypted)
    temp_dir = tempfile.mkdtemp(prefix="github-drive-parts-")
    try:
        if len(parts) == 1:
            raw_path = os.path.join(temp_dir, "asset.bin")
            part = parts[0]
            _retry(
                f"download {item['relative_path']}",
                retries,
                lambda: client.download_asset(part["asset_id"], raw_path),
            )
            if is_encrypted:
                crypto.decrypt_file(raw_path, output_path, encode_key)
            else:
                shutil.move(raw_path, output_path)
            return

        with open(output_path, "wb") as out:
            for index, part in enumerate(parts):
                raw_path = os.path.join(temp_dir, f"part-{index:04d}.bin")
                _retry(
                    f"download {item['relative_path']} part {index + 1}/{len(parts)}",
                    retries,
                    lambda pid=part["asset_id"], path=raw_path: client.download_asset(pid, path),
                )
                if is_encrypted:
                    decrypted_path = raw_path + ".dec"
                    crypto.decrypt_file(raw_path, decrypted_path, encode_key)
                    os.unlink(raw_path)
                    chunk_path = decrypted_path
                else:
                    chunk_path = raw_path
                with open(chunk_path, "rb") as src:
                    while True:
                        buf = src.read(COPY_BUFFER)
                        if not buf:
                            break
                        out.write(buf)
                os.unlink(chunk_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def delete_archive(
    release_id: Optional[int] = None,
    tag: Optional[str] = None,
    archive_id: Optional[str] = None,
    delete_tag: bool = True,
    client: Optional[GitHubClient] = None,
) -> Dict:
    client = client or get_client()
    release = _resolve_release(client, release_id=release_id, tag=tag, archive_id=archive_id)
    client.delete_release(release["id"])
    if delete_tag and release.get("tag_name"):
        client.delete_tag(release["tag_name"])
    return {"release_id": release["id"], "tag": release.get("tag_name", "")}


# ── helpers ───────────────────────────────────────────────────────────────────

COVER_ASSET_NAME = "_cover.jpg"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_CACHE_LOCK = None


def _load_archive_snapshot(
    client: GitHubClient,
    release_id: Optional[int],
    tag: Optional[str],
    archive_id: Optional[str],
) -> Tuple[Dict, Dict, List[Dict], Dict[str, Dict], Optional[Dict], List[Dict], bool, str]:
    release = _resolve_release(client, release_id=release_id, tag=tag, archive_id=archive_id)
    archive_meta = decode_archive_body(release.get("body") or "")
    if not archive_meta:
        raise RuntimeError(f"Release {release.get('tag_name')} is not a github-drive archive.")
    assets = client.list_release_assets(release["id"])
    by_name = {asset["name"]: asset for asset in assets}
    manifest = None
    if MANIFEST_ASSET_NAME in by_name:
        try:
            manifest_bytes = client.download_asset_bytes(by_name[MANIFEST_ASSET_NAME]["id"])
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (GitHubError, json.JSONDecodeError, UnicodeDecodeError):
            manifest = None
    items, encrypted, storage_mode, _progress_total = _build_download_items(client, release["id"], assets, archive_meta)
    return release, archive_meta, assets, by_name, manifest, items, encrypted, storage_mode


def _flatten_archive_entries(items: List[Dict], storage_mode: str) -> List[Dict]:
    from . import thumbnails

    entries: List[Dict] = []
    if storage_mode == STORAGE_MODE_BUNDLE_ASSETS:
        for item in items:
            for member in item.get("members") or []:
                relative_path = member.get("relative_path") or ""
                ext = Path(relative_path).suffix.lower()
                entries.append(
                    {
                        "relative_path": relative_path,
                        "original_size": int(member.get("original_size") or 0),
                        "content_type": member.get("content_type") or "application/octet-stream",
                        "kind": thumbnails.classify_extension(ext),
                        "previewable": ext in thumbnails.IMAGE_EXTENSIONS,
                    }
                )
    else:
        for item in items:
            relative_path = item.get("relative_path") or ""
            ext = Path(relative_path).suffix.lower()
            entries.append(
                {
                    "relative_path": relative_path,
                    "original_size": int(item.get("original_size") or 0),
                    "content_type": item.get("content_type") or "application/octet-stream",
                    "kind": thumbnails.classify_extension(ext),
                    "previewable": ext in thumbnails.IMAGE_EXTENSIONS,
                }
            )
    entries.sort(key=lambda entry: entry["relative_path"].lower())
    return entries


def _find_archive_entry(items: List[Dict], storage_mode: str, relative_path: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    needle = str(relative_path or "")
    if storage_mode == STORAGE_MODE_BUNDLE_ASSETS:
        for item in items:
            for member in item.get("members") or []:
                if member.get("relative_path") == needle:
                    return item, member
        return None, None
    for item in items:
        if item.get("relative_path") == needle:
            return item, None
    return None, None


def _read_archive_entry_bytes(
    client: GitHubClient,
    item: Dict,
    relative_path: str,
    member: Optional[Dict],
    encrypted: bool,
    encode_key: Optional[bytes],
    retries: int = 3,
) -> Tuple[bytes, str]:
    temp_dir = tempfile.mkdtemp(prefix="github-drive-read-")
    try:
        if member is not None:
            bundle_path = os.path.join(temp_dir, "bundle.zip")
            _materialize_download_parts(
                client=client,
                item=item,
                output_path=bundle_path,
                encrypted=encrypted,
                encode_key=encode_key,
                retries=retries,
            )
            with zipfile.ZipFile(bundle_path, "r") as archive:
                with archive.open(relative_path, "r") as src:
                    return src.read(), member.get("content_type") or "application/octet-stream"

        file_path = os.path.join(temp_dir, "entry.bin")
        _materialize_download_parts(
            client=client,
            item=item,
            output_path=file_path,
            encrypted=encrypted,
            encode_key=encode_key,
            retries=retries,
        )
        with open(file_path, "rb") as handle:
            return handle.read(), item.get("content_type") or "application/octet-stream"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _manifest_item_from_download_item(item: Dict) -> Dict:
    return {
        "order": int(item.get("order") or 0),
        "asset_name": item.get("asset_name") or "",
        "asset_id": int(item.get("asset_id") or 0),
        "relative_path": item.get("relative_path") or "",
        "original_size": int(item.get("original_size") or 0),
        "source_sha256": item.get("source_sha256") or "",
        "encrypted": bool(item.get("encrypted")),
        "content_type": item.get("content_type") or "application/octet-stream",
        "parts": list(item.get("parts") or []),
        "members": list(item.get("members") or []),
    }


def _classify_relative_paths(relative_paths: List[str]) -> Dict[str, int]:
    from . import thumbnails

    counts = {"image": 0, "video": 0, "audio": 0, "document": 0, "archive": 0, "code": 0, "other": 0}
    for relative_path in relative_paths:
        counts[thumbnails.classify_extension(Path(relative_path).suffix.lower())] += 1
    return counts


def _is_image_path(relative_path: str) -> bool:
    from . import thumbnails

    return Path(relative_path).suffix.lower() in thumbnails.IMAGE_EXTENSIONS


def _sanitize_for_asset_name(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value).strip("-.")
    return cleaned or "file"


def _bundle_relative_name(source_name: str) -> str:
    safe = _sanitize_for_asset_name(source_name)[:120]
    return f"{safe}{BUNDLE_ARCHIVE_SUFFIX}"


def _asset_name_for(order: int, relative_path: str, encrypted: bool) -> str:
    flat = relative_path.replace("/", "__").replace("\\", "__")
    safe = _sanitize_for_asset_name(flat)[:180]
    suffix = ENCRYPTED_SUFFIX if encrypted else ""
    return f"{order:04d}-{safe}{suffix}"


def _part_asset_name_for(order: int, relative_path: str, chunk_index: int, encrypted: bool) -> str:
    flat = relative_path.replace("/", "__").replace("\\", "__")
    safe = _sanitize_for_asset_name(flat)[:160]
    suffix = ENCRYPTED_SUFFIX if encrypted else ""
    return f"{order:04d}-{safe}.part{chunk_index:04d}{suffix}"


def _decode_asset_name(asset_name: str, archive_encrypted: bool):
    """Best-effort fallback when no manifest is available."""
    name = asset_name
    encrypted = name.endswith(ENCRYPTED_SUFFIX) or archive_encrypted
    if name.endswith(ENCRYPTED_SUFFIX):
        name = name[: -len(ENCRYPTED_SUFFIX)]
    order = 0
    if "-" in name:
        head, rest = name.split("-", 1)
        if head.isdigit():
            order = int(head)
            name = rest
    relative_path = name.replace("__", "/")
    return order, relative_path, encrypted


def _make_archive_title(source_name: str, total_items: int) -> str:
    safe_source = re.sub(r"\s+", " ", source_name).strip() or "archive"
    return f"GitHub Drive | {safe_source} | {total_items} items"


def _resolve_destination_path(destination: str, relative_path: str) -> Path:
    base = Path(destination).expanduser().resolve()
    target = (base / Path(relative_path)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise RuntimeError(f"Refusing to write outside destination: {relative_path!r}") from exc
    return target


def _retry(operation_name: str, retries: int, fn):
    attempts = max(1, int(retries))
    delay = 1.0
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(delay)
            delay = min(delay * 2.0, 8.0)
    raise RuntimeError(f"{operation_name} failed after {attempts} attempt(s): {last_error}") from last_error


def _sha256_file(path: str) -> str:
    digest = sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
