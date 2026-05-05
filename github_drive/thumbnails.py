"""Cover-thumbnail generation and content-type classification for archive entries.

The generated cover is uploaded as a release asset named `_cover.jpg`. It is best-effort —
if the runtime can't decode the source media (HEIC/RAW without plugins, corrupt files,
missing ffmpeg for videos, etc.), we silently fall back to no cover. The frontend handles
a missing cover by showing the generic archive icon.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

COVER_ASSET_NAME = "_cover.jpg"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".m2ts", ".wmv", ".flv"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus", ".m4a"}
DOC_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv", ".rtf", ".odt"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
                   ".go", ".rs", ".rb", ".php", ".sh", ".html", ".css", ".json", ".yaml", ".yml", ".toml"}


def classify_extension(ext: str) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXTENSIONS: return "image"
    if ext in VIDEO_EXTENSIONS: return "video"
    if ext in AUDIO_EXTENSIONS: return "audio"
    if ext in DOC_EXTENSIONS: return "document"
    if ext in ARCHIVE_EXTENSIONS: return "archive"
    if ext in CODE_EXTENSIONS: return "code"
    return "other"


def classify_entries(entries: List[Dict]) -> Dict[str, int]:
    """Return a count of each kind for the entries list."""
    counts = {"image": 0, "video": 0, "audio": 0, "document": 0, "archive": 0, "code": 0, "other": 0}
    for entry in entries:
        ext = Path(entry["relative_path"]).suffix.lower()
        counts[classify_extension(ext)] += 1
    return counts


def first_image_entry(entries: List[Dict]) -> Optional[Dict]:
    for entry in entries:
        ext = Path(entry["relative_path"]).suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return entry
    return None


def first_visual_entry(entries: List[Dict]) -> Optional[Dict]:
    for entry in entries:
        ext = Path(entry["relative_path"]).suffix.lower()
        if ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS:
            return entry
    return None


def first_image_asset(assets: List[Dict]) -> Optional[Dict]:
    for asset in assets:
        name = asset.get("name") or ""
        if name == COVER_ASSET_NAME or name == "_manifest.json":
            continue
        ext = Path(name).suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return asset
    return None


def first_visual_asset(assets: List[Dict]) -> Optional[Dict]:
    for asset in assets:
        name = asset.get("name") or ""
        if name == COVER_ASSET_NAME or name == "_manifest.json":
            continue
        ext = Path(name).suffix.lower()
        if ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS:
            return asset
    return None


def make_cover_jpeg(src_path: str, size: int = 480) -> Optional[bytes]:
    """Center-crop and resize `src_path` into a square JPEG. Returns None on failure."""
    try:
        with open(src_path, "rb") as handle:
            return make_cover_jpeg_from_bytes(handle.read(), size=size)
    except Exception:
        return None


def make_video_cover_jpeg(src_path: str, size: int = 480, seek_seconds: float = 0.5) -> Optional[bytes]:
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        return None
    temp_dir = tempfile.mkdtemp(prefix="github-drive-video-cover-")
    frame_path = os.path.join(temp_dir, "frame.jpg")
    try:
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", str(max(0.0, seek_seconds)),
            "-i", src_path,
            "-frames:v", "1",
            "-vf", f"thumbnail,scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}",
            frame_path,
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(frame_path, "rb") as handle:
            return handle.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def make_cover_for_path(src_path: str, size: int = 480) -> Optional[bytes]:
    ext = Path(src_path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return make_cover_jpeg(src_path, size=size)
    if ext in VIDEO_EXTENSIONS:
        return make_video_cover_jpeg(src_path, size=size)
    return None


def make_cover_from_bytes(payload: bytes, suffix: str = "", size: int = 480) -> Optional[bytes]:
    ext = (suffix or "").lower()
    if ext in VIDEO_EXTENSIONS:
        temp_dir = tempfile.mkdtemp(prefix="github-drive-video-cover-bytes-")
        temp_path = os.path.join(temp_dir, f"source{ext or '.bin'}")
        try:
            with open(temp_path, "wb") as handle:
                handle.write(payload)
            return make_video_cover_jpeg(temp_path, size=size)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return make_cover_jpeg_from_bytes(payload, size=size)


def preview_thumb_supported(ext: str) -> bool:
    ext = ext.lower()
    return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS


def _ffmpeg_executable() -> Optional[str]:
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


def make_cover_jpeg_from_bytes(payload: bytes, size: int = 480) -> Optional[bytes]:
    """Center-crop and resize image bytes into a square JPEG. Returns None on failure."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(BytesIO(payload)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=80, optimize=True)
            return buf.getvalue()
    except Exception:
        return None
