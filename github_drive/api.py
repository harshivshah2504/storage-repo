import json
import os
import random
import time
from datetime import datetime, timezone
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
        super().__init__(f"GitHub API error {status}: {message}")
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
        url = self._repo_url("/releases")
        params = {"per_page": 100}
        while True:
            response = self._request("GET", url, params=params)
            for item in response.json():
                yield item
            link = response.headers.get("Link", "")
            next_url = _parse_next_link(link)
            if not next_url:
                break
            url = next_url
            params = None

    def get_release_by_tag(self, tag: str) -> Optional[Dict]:
        try:
            return self._request("GET", self._repo_url(f"/releases/tags/{tag}")).json()
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def get_release(self, release_id: int) -> Dict:
        return self._request("GET", self._repo_url(f"/releases/{release_id}")).json()

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
        return self._request("POST", self._repo_url("/releases"), json=payload).json()

    def update_release(self, release_id: int, **fields) -> Dict:
        return self._request("PATCH", self._repo_url(f"/releases/{release_id}"), json=fields).json()

    def delete_release(self, release_id: int) -> None:
        self._request("DELETE", self._repo_url(f"/releases/{release_id}"))

    def delete_tag(self, tag: str) -> None:
        try:
            self._request("DELETE", self._repo_url(f"/git/refs/tags/{tag}"))
        except GitHubError as exc:
            if exc.status != 404:
                raise

    def list_release_assets(self, release_id: int) -> List[Dict]:
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
        return response.json()

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
        return response.json()

    def delete_asset(self, asset_id: int) -> None:
        self._request("DELETE", self._repo_url(f"/releases/assets/{asset_id}"))

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

    def download_asset_bytes(self, asset_id: int) -> bytes:
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
        return response.content


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
                "created_at": release.get("created_at", ""),
                "updated_at": release.get("updated_at", ""),
                "archive": meta,
            }
        )
    archives.sort(key=lambda item: item["archive"].get("created_at") or item["created_at"] or "", reverse=True)
    return archives


def archive_tag_for(archive_id: str) -> str:
    return f"{ARCHIVE_TAG_PREFIX}{archive_id.lower()}"
