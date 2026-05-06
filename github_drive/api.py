import json
import os
import random
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

GITHUB_API_BASE = "https://api.github.com"
GITHUB_UPLOADS_BASE = "https://uploads.github.com"
DEFAULT_API_VERSION = "2022-11-28"
ARCHIVE_TAG_PREFIX = "github-drive-"
STORAGE_FORMAT = "github-drive-archive"
METADATA_VERSION = 1
ARCHIVE_MARKER = "GITHUB_DRIVE_ARCHIVE="
MANIFEST_ASSET_NAME = "_manifest.json"
_CACHE_LOCK = threading.Lock()
_RELEASES_CACHE: Dict[Tuple[str, str], Dict] = {}
_RELEASES_PAGE_CACHE: Dict[Tuple[str, int, int], Dict] = {}
_RELEASE_CACHE: Dict[Tuple[str, int], Dict] = {}
_RELEASE_TAG_CACHE: Dict[Tuple[str, str], Dict] = {}
_ASSETS_CACHE: Dict[Tuple[str, int], Dict] = {}
_ASSET_BYTES_CACHE: Dict[Tuple[str, int], Dict] = {}
_ASSET_TO_RELEASE: Dict[Tuple[str, int], int] = {}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def encode_archive_body(metadata: Dict) -> str:
    payload = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True)
    return (
        "GitHub Drive archive. Do not edit the marker line below; it is parsed by the tool.\n\n"
        f"```\n{ARCHIVE_MARKER}{payload}\n```\n"
    )


def decode_archive_body(text: str) -> Optional[Dict]:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(ARCHIVE_MARKER):
            try:
                return json.loads(line[len(ARCHIVE_MARKER):])
            except json.JSONDecodeError:
                return None
    return None


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, response_body: str = ""):
        super().__init__(_format_github_error(status, message, response_body))
        self.status = status
        self.response_body = response_body


class GitHubClient:
    """Minimal GitHub REST API client for releases + assets."""

    def __init__(self, token: str, owner: str, repo: str, timeout: int = 60):
        if not token:
            raise RuntimeError("A GitHub token is required.")
        if not owner or not repo:
            raise RuntimeError("Both repository owner and name are required.")
        self.token = token
        self.owner = owner
        self.repo = repo
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(self._default_headers())
        self._cache_namespace = f"{sha256(self.token.encode('utf-8')).hexdigest()[:16]}:{self.owner}/{self.repo}"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": DEFAULT_API_VERSION,
            "User-Agent": "github-drive",
        }

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        return self._request_with_retries(
            f"{method} {url}",
            lambda: self._session.request(method, url, timeout=timeout, **kwargs),
        )

    def _request_with_retries(self, operation_name: str, send: Callable[[], requests.Response]) -> requests.Response:
        max_attempts = 6
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            response: Optional[requests.Response] = None
            try:
                response = send()
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                time.sleep(self._retry_delay(attempt=attempt, exc=exc))
                continue

            if response.status_code < 400:
                return response

            if self._is_retryable_response(response) and attempt < max_attempts:
                delay = self._retry_delay(attempt=attempt, response=response)
                response.close()
                time.sleep(delay)
                continue

            raise GitHubError(response.status_code, response.reason or "request failed", response.text)

        raise GitHubError(0, f"{operation_name} failed after {max_attempts} attempts", str(last_error or ""))

    def _is_retryable_response(self, response: requests.Response) -> bool:
        if response.status_code in {429, 500, 502, 503, 504}:
            return True
        if response.status_code != 403:
            return False
        body = (response.text or "").lower()
        if "secondary rate limit" in body or "rate limit" in body:
            return True
        if response.headers.get("Retry-After"):
            return True
        return response.headers.get("X-RateLimit-Remaining") == "0"

    def _retry_delay(
        self,
        attempt: int,
        response: Optional[requests.Response] = None,
        exc: Optional[Exception] = None,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(1.0, min(float(retry_after), 300.0))
                except ValueError:
                    pass
            if response.headers.get("X-RateLimit-Remaining") == "0":
                reset = response.headers.get("X-RateLimit-Reset")
                if reset:
                    try:
                        wait = max(1.0, float(reset) - time.time())
                        return min(wait, 300.0)
                    except ValueError:
                        pass
            body = (response.text or "").lower()
            if "secondary rate limit" in body:
                base = max(5.0, min(2 ** (attempt - 1), 60.0))
                return min(base + random.uniform(0.0, 2.0), 120.0)

        if exc is not None:
            base = min(2 ** (attempt - 1), 30.0)
            return min(base + random.uniform(0.0, 1.0), 60.0)

        base = min(2 ** (attempt - 1), 30.0)
        return min(base + random.uniform(0.0, 1.0), 60.0)

    def _repo_url(self, suffix: str) -> str:
        return f"{GITHUB_API_BASE}/repos/{self.owner}/{self.repo}{suffix}"

    def repo_info(self) -> Dict:
        return self._request("GET", self._repo_url("")).json()

    def ensure_repo(self, private: bool = True, description: str = "GitHub Drive archives") -> Dict:
        try:
            return self.repo_info()
        except GitHubError as exc:
            if exc.status != 404:
                raise
        body = {"name": self.repo, "private": bool(private), "description": description, "auto_init": True}
        url = f"{GITHUB_API_BASE}/user/repos"
        return self._request("POST", url, json=body).json()

    def viewer_login(self) -> str:
        return self._request("GET", f"{GITHUB_API_BASE}/user").json()["login"]

    def list_releases(self) -> Iterator[Dict]:
        cached = _cache_get(_RELEASES_CACHE, (self._cache_namespace, "all"), _releases_cache_ttl())
        if cached is not None:
            for item in cached:
                yield item
            return

        url = self._repo_url("/releases")
        params = {"per_page": 100}
        releases: List[Dict] = []
        while True:
            response = self._request("GET", url, params=params)
            page_items = response.json()
            releases.extend(page_items)
            for item in page_items:
                yield item
            link = response.headers.get("Link", "")
            next_url = _parse_next_link(link)
            if not next_url:
                break
            url = next_url
            params = None
        _cache_set(_RELEASES_CACHE, (self._cache_namespace, "all"), releases)
        self._index_releases(releases)

    def list_releases_page(self, page: int = 1, per_page: int = 24) -> Tuple[List[Dict], bool]:
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        cached = _cache_get(
            _RELEASES_PAGE_CACHE,
            (self._cache_namespace, page, per_page),
            _releases_cache_ttl(),
        )
        if cached is not None:
            return list(cached.get("items") or []), bool(cached.get("has_more"))

        response = self._request(
            "GET",
            self._repo_url("/releases"),
            params={"per_page": per_page, "page": page},
        )
        items = response.json()
        has_more = bool(_parse_next_link(response.headers.get("Link", "")))
        _cache_set(
            _RELEASES_PAGE_CACHE,
            (self._cache_namespace, page, per_page),
            {"items": items, "has_more": has_more},
        )
        self._index_releases(items)
        return items, has_more

    def get_release_by_tag(self, tag: str) -> Optional[Dict]:
        cached = _cache_get(_RELEASE_TAG_CACHE, (self._cache_namespace, tag), _release_cache_ttl())
        if cached is not None:
            return cached
        releases = _cache_get(_RELEASES_CACHE, (self._cache_namespace, "all"), _releases_cache_ttl())
        if releases is not None:
            for release in releases:
                if (release.get("tag_name") or "") == tag:
                    _cache_set(_RELEASE_TAG_CACHE, (self._cache_namespace, tag), release)
                    _cache_set(_RELEASE_CACHE, (self._cache_namespace, int(release["id"])), release)
                    return release
        try:
            release = self._request("GET", self._repo_url(f"/releases/tags/{tag}")).json()
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        _cache_set(_RELEASE_TAG_CACHE, (self._cache_namespace, tag), release)
        _cache_set(_RELEASE_CACHE, (self._cache_namespace, int(release["id"])), release)
        return release

    def get_release(self, release_id: int) -> Dict:
        cached = _cache_get(_RELEASE_CACHE, (self._cache_namespace, int(release_id)), _release_cache_ttl())
        if cached is not None:
            return cached
        releases = _cache_get(_RELEASES_CACHE, (self._cache_namespace, "all"), _releases_cache_ttl())
        if releases is not None:
            for release in releases:
                if int(release.get("id") or 0) == int(release_id):
                    _cache_set(_RELEASE_CACHE, (self._cache_namespace, int(release_id)), release)
                    tag_name = (release.get("tag_name") or "").strip()
                    if tag_name:
                        _cache_set(_RELEASE_TAG_CACHE, (self._cache_namespace, tag_name), release)
                    return release
        release = self._request("GET", self._repo_url(f"/releases/{release_id}")).json()
        _cache_set(_RELEASE_CACHE, (self._cache_namespace, int(release_id)), release)
        tag_name = (release.get("tag_name") or "").strip()
        if tag_name:
            _cache_set(_RELEASE_TAG_CACHE, (self._cache_namespace, tag_name), release)
        return release

    def create_release(
        self,
        tag: str,
        name: str,
        body: str,
        draft: bool = False,
        prerelease: bool = False,
        target_commitish: Optional[str] = None,
    ) -> Dict:
        payload = {
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
        }
        if target_commitish:
            payload["target_commitish"] = target_commitish
        created = self._request("POST", self._repo_url("/releases"), json=payload).json()
        self._invalidate_repo_metadata_cache()
        self._invalidate_release_assets_cache(int(created["id"]))
        return created

    def update_release(self, release_id: int, **fields) -> Dict:
        updated = self._request("PATCH", self._repo_url(f"/releases/{release_id}"), json=fields).json()
        self._invalidate_repo_metadata_cache()
        self._invalidate_release_assets_cache(int(release_id))
        return updated

    def delete_release(self, release_id: int) -> None:
        self._request("DELETE", self._repo_url(f"/releases/{release_id}"))
        self._invalidate_release_assets_cache(int(release_id))
        self._invalidate_repo_metadata_cache()

    def delete_tag(self, tag: str) -> None:
        try:
            self._request("DELETE", self._repo_url(f"/git/refs/tags/{tag}"))
        except GitHubError as exc:
            if exc.status != 404:
                raise
        self._invalidate_repo_metadata_cache()

    def list_release_assets(self, release_id: int) -> List[Dict]:
        release_id = int(release_id)
        cached = _cache_get(_ASSETS_CACHE, (self._cache_namespace, release_id), _assets_cache_ttl())
        if cached is not None:
            return cached
        assets: List[Dict] = []
        url = self._repo_url(f"/releases/{release_id}/assets")
        params = {"per_page": 100}
        while True:
            response = self._request("GET", url, params=params)
            assets.extend(response.json())
            next_url = _parse_next_link(response.headers.get("Link", ""))
            if not next_url:
                break
            url = next_url
            params = None
        _cache_set(_ASSETS_CACHE, (self._cache_namespace, release_id), assets)
        with _CACHE_LOCK:
            for asset in assets:
                _ASSET_TO_RELEASE[(self._cache_namespace, int(asset["id"]))] = release_id
        return assets

    def upload_asset(
        self,
        release_id: int,
        asset_name: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        label: Optional[str] = None,
    ) -> Dict:
        url = f"{GITHUB_UPLOADS_BASE}/repos/{self.owner}/{self.repo}/releases/{release_id}/assets"
        params = {"name": asset_name}
        if label:
            params["label"] = label
        file_size = os.path.getsize(file_path)
        headers = dict(self._default_headers())
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(file_size)

        def send() -> requests.Response:
            with open(file_path, "rb") as handle:
                return self._session.post(
                    url,
                    params=params,
                    headers=headers,
                    data=handle,
                    timeout=max(self.timeout, 600),
                )

        response = self._request_with_retries(f"upload asset {asset_name}", send)
        uploaded = response.json()
        with _CACHE_LOCK:
            _ASSET_TO_RELEASE[(self._cache_namespace, int(uploaded["id"]))] = int(release_id)
        self._invalidate_release_assets_cache(int(release_id))
        self._invalidate_repo_metadata_cache()
        return uploaded

    def upload_asset_stream(
        self,
        release_id: int,
        asset_name: str,
        stream,
        content_length: int,
        content_type: str = "application/octet-stream",
    ) -> Dict:
        url = f"{GITHUB_UPLOADS_BASE}/repos/{self.owner}/{self.repo}/releases/{release_id}/assets"
        headers = dict(self._default_headers())
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(int(content_length))
        try:
            start_pos = int(stream.tell())
        except Exception:
            start_pos = 0

        def send() -> requests.Response:
            try:
                stream.seek(start_pos)
            except Exception:
                pass
            return self._session.post(
                url,
                params={"name": asset_name},
                headers=headers,
                data=stream,
                timeout=max(self.timeout, 600),
            )

        response = self._request_with_retries(f"upload asset {asset_name}", send)
        uploaded = response.json()
        with _CACHE_LOCK:
            _ASSET_TO_RELEASE[(self._cache_namespace, int(uploaded["id"]))] = int(release_id)
        self._invalidate_release_assets_cache(int(release_id))
        self._invalidate_repo_metadata_cache()
        return uploaded

    def upload_asset_bytes(
        self,
        release_id: int,
        asset_name: str,
        payload: bytes,
        content_type: str = "application/octet-stream",
    ) -> Dict:
        url = f"{GITHUB_UPLOADS_BASE}/repos/{self.owner}/{self.repo}/releases/{release_id}/assets"
        headers = dict(self._default_headers())
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(payload))
        response = self._request_with_retries(
            f"upload asset {asset_name}",
            lambda: self._session.post(
                url,
                params={"name": asset_name},
                headers=headers,
                data=payload,
                timeout=max(self.timeout, 600),
            ),
        )
        uploaded = response.json()
        with _CACHE_LOCK:
            _ASSET_TO_RELEASE[(self._cache_namespace, int(uploaded["id"]))] = int(release_id)
        self._invalidate_release_assets_cache(int(release_id))
        self._invalidate_repo_metadata_cache()
        return uploaded

    def delete_asset(self, asset_id: int) -> None:
        self._request("DELETE", self._repo_url(f"/releases/assets/{asset_id}"))
        release_id = None
        with _CACHE_LOCK:
            release_id = _ASSET_TO_RELEASE.pop((self._cache_namespace, int(asset_id)), None)
            _ASSET_BYTES_CACHE.pop((self._cache_namespace, int(asset_id)), None)
        if release_id is not None:
            self._invalidate_release_assets_cache(int(release_id))
            self._invalidate_repo_metadata_cache()

    def download_asset(self, asset_id: int, output_path: str, chunk_size: int = 1024 * 1024) -> None:
        url = self._repo_url(f"/releases/assets/{asset_id}")
        headers = dict(self._default_headers())
        headers["Accept"] = "application/octet-stream"
        response = self._request_with_retries(
            f"download asset {asset_id}",
            lambda: self._session.get(
                url,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=max(self.timeout, 600),
            ),
        )
        with response:
            if response.status_code >= 400:
                raise GitHubError(response.status_code, response.reason or "asset download failed", response.text[:500])
            with open(output_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        handle.write(chunk)

    def download_asset_bytes(self, asset_id: int, use_cache: bool = False) -> bytes:
        asset_id = int(asset_id)
        if use_cache:
            cached = _cache_get(_ASSET_BYTES_CACHE, (self._cache_namespace, asset_id), _asset_bytes_cache_ttl())
            if cached is not None:
                return cached
        url = self._repo_url(f"/releases/assets/{asset_id}")
        headers = dict(self._default_headers())
        headers["Accept"] = "application/octet-stream"
        response = self._request_with_retries(
            f"download asset {asset_id}",
            lambda: self._session.get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=max(self.timeout, 600),
            ),
        )
        payload = response.content
        if use_cache and len(payload) <= _asset_bytes_cache_max_bytes():
            _cache_set(_ASSET_BYTES_CACHE, (self._cache_namespace, asset_id), payload)
        return payload

    def _invalidate_repo_metadata_cache(self) -> None:
        with _CACHE_LOCK:
            _RELEASES_CACHE.pop((self._cache_namespace, "all"), None)
            for key in [key for key in _RELEASES_PAGE_CACHE if key[0] == self._cache_namespace]:
                _RELEASES_PAGE_CACHE.pop(key, None)
            for key in [key for key in _RELEASE_CACHE if key[0] == self._cache_namespace]:
                _RELEASE_CACHE.pop(key, None)
            for key in [key for key in _RELEASE_TAG_CACHE if key[0] == self._cache_namespace]:
                _RELEASE_TAG_CACHE.pop(key, None)

    def _invalidate_release_assets_cache(self, release_id: int) -> None:
        release_id = int(release_id)
        with _CACHE_LOCK:
            _ASSETS_CACHE.pop((self._cache_namespace, release_id), None)
            asset_keys = [
                key for key, value in _ASSET_TO_RELEASE.items()
                if key[0] == self._cache_namespace and int(value) == release_id
            ]
            for namespace_key in asset_keys:
                _ASSET_TO_RELEASE.pop(namespace_key, None)
                _ASSET_BYTES_CACHE.pop(namespace_key, None)

    def _index_releases(self, releases: List[Dict]) -> None:
        ttl = _release_cache_ttl()
        if ttl <= 0:
            return
        with _CACHE_LOCK:
            for release in releases:
                release_id = int(release["id"])
                _RELEASE_CACHE[(self._cache_namespace, release_id)] = {
                    "expires_at": time.time() + ttl,
                    "value": deepcopy(release),
                }
                tag_name = (release.get("tag_name") or "").strip()
                if tag_name:
                    _RELEASE_TAG_CACHE[(self._cache_namespace, tag_name)] = {
                        "expires_at": time.time() + ttl,
                        "value": deepcopy(release),
                    }


def _parse_next_link(link_header: str) -> Optional[str]:
    if not link_header:
        return None
    parts = [piece.strip() for piece in link_header.split(",")]
    for part in parts:
        segments = [segment.strip() for segment in part.split(";")]
        if len(segments) < 2:
            continue
        url_segment = segments[0]
        rel_segments = segments[1:]
        if 'rel="next"' in rel_segments and url_segment.startswith("<") and url_segment.endswith(">"):
            return url_segment[1:-1]
    return None


def parse_owner_repo(slug: str) -> Tuple[str, str]:
    """Accept 'owner/repo' and return (owner, repo)."""
    if not slug or "/" not in slug:
        raise RuntimeError("Repository must be specified as 'owner/repo'.")
    owner, repo = slug.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        raise RuntimeError("Repository must be specified as 'owner/repo'.")
    return owner, repo


def list_drive_archives(client: GitHubClient) -> List[Dict]:
    archives: List[Dict] = []
    for release in client.list_releases():
        meta = decode_archive_body(release.get("body") or "")
        if not meta:
            continue
        archives.append(
            {
                "release_id": release["id"],
                "tag": release.get("tag_name", ""),
                "name": release.get("name") or release.get("tag_name") or "",
                "html_url": release.get("html_url"),
                "draft": release.get("draft", False),
                "prerelease": release.get("prerelease", False),
                "asset_count": len(release.get("assets") or []),
                "total_asset_bytes": sum(int(asset.get("size") or 0) for asset in (release.get("assets") or [])),
                "created_at": release.get("created_at", ""),
                "updated_at": release.get("updated_at", ""),
                "archive": meta,
            }
        )
    archives.sort(key=lambda item: item["archive"].get("created_at") or item["created_at"] or "", reverse=True)
    return archives


def list_drive_archives_page(
    client: GitHubClient,
    page: int = 1,
    per_page: int = 24,
) -> Tuple[List[Dict], bool]:
    releases, has_more = client.list_releases_page(page=page, per_page=per_page)
    archives: List[Dict] = []
    for release in releases:
        meta = decode_archive_body(release.get("body") or "")
        if not meta:
            continue
        archives.append(
            {
                "release_id": release["id"],
                "tag": release.get("tag_name", ""),
                "name": release.get("name") or release.get("tag_name") or "",
                "html_url": release.get("html_url"),
                "draft": release.get("draft", False),
                "prerelease": release.get("prerelease", False),
                "asset_count": len(release.get("assets") or []),
                "total_asset_bytes": sum(int(asset.get("size") or 0) for asset in (release.get("assets") or [])),
                "created_at": release.get("created_at", ""),
                "updated_at": release.get("updated_at", ""),
                "archive": meta,
            }
        )
    archives.sort(key=lambda item: item["archive"].get("created_at") or item["created_at"] or "", reverse=True)
    return archives, has_more


def _cache_get(bucket: Dict, key: Tuple, ttl: float):
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        entry = bucket.get(key)
        if not entry:
            return None
        if float(entry.get("expires_at") or 0.0) < time.time():
            bucket.pop(key, None)
            return None
        value = entry.get("value")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return deepcopy(value)


def _cache_set(bucket: Dict, key: Tuple, value) -> None:
    ttl = 0.0
    if bucket is _RELEASES_CACHE:
        ttl = _releases_cache_ttl()
    elif bucket is _RELEASE_CACHE or bucket is _RELEASE_TAG_CACHE:
        ttl = _release_cache_ttl()
    elif bucket is _ASSETS_CACHE:
        ttl = _assets_cache_ttl()
    elif bucket is _ASSET_BYTES_CACHE:
        ttl = _asset_bytes_cache_ttl()
    if ttl <= 0:
        return
    stored = bytes(value) if isinstance(value, (bytes, bytearray)) else deepcopy(value)
    with _CACHE_LOCK:
        bucket[key] = {"expires_at": time.time() + ttl, "value": stored}


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _releases_cache_ttl() -> float:
    return _env_float("GITHUB_DRIVE_RELEASES_CACHE_TTL_SECONDS", 30.0)


def _release_cache_ttl() -> float:
    return _env_float("GITHUB_DRIVE_RELEASE_CACHE_TTL_SECONDS", 30.0)


def _assets_cache_ttl() -> float:
    return _env_float("GITHUB_DRIVE_RELEASE_ASSETS_CACHE_TTL_SECONDS", 30.0)


def _asset_bytes_cache_ttl() -> float:
    return _env_float("GITHUB_DRIVE_ASSET_BYTES_CACHE_TTL_SECONDS", 600.0)


def _asset_bytes_cache_max_bytes() -> int:
    return _env_int("GITHUB_DRIVE_ASSET_BYTES_CACHE_MAX_BYTES", 2 * 1024 * 1024)


def archive_tag_for(archive_id: str) -> str:
    return f"{ARCHIVE_TAG_PREFIX}{archive_id.lower()}"


def _format_github_error(status: int, message: str, response_body: str = "") -> str:
    details: List[str] = []
    parsed = _parse_github_error_body(response_body)
    if parsed:
        api_message = (parsed.get("message") or "").strip()
        if api_message and api_message.lower() != str(message or "").strip().lower():
            details.append(api_message)
        for item in parsed.get("errors") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            field = str(item.get("field") or "").strip()
            resource = str(item.get("resource") or "").strip()
            entry_message = str(item.get("message") or "").strip()
            parts = [part for part in [resource, field, code] if part]
            summary = ".".join(parts)
            if entry_message and summary:
                details.append(f"{summary}: {entry_message}")
            elif entry_message:
                details.append(entry_message)
            elif summary:
                details.append(summary)
    if details:
        return f"GitHub API error {status}: {'; '.join(details[:3])}"
    return f"GitHub API error {status}: {message}"


def _parse_github_error_body(response_body: str) -> Optional[Dict]:
    if not response_body:
        return None
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
