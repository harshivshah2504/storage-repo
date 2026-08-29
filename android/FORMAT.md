# github-drive Archive Wire-Format Specification

Derived by reading `/tmp/repo/github_drive/{storage.py,crypto.py,api.py,thumbnails.py,limits.py,webapp.py,users.py,auth_manager.py,main.py,static/app.js}`.
Everything below is quoted from or directly traced to the source. Sections marked **AMBIGUITY** flag
places where the code does not pin down a byte-exact answer, or where the question's premise does not
match the implementation.

Target: a Kotlin/Android implementation that produces and consumes archives interoperable with this web app.

---

## 0. Vocabulary and top-level model

An **archive** is one GitHub **Release** in a user's repo:

- Release **body** carries a compact JSON metadata blob on a marker line (`GITHUB_DRIVE_ARCHIVE=`).
- Release **assets** carry the payload:
  - one asset per source file (`file-assets` mode), possibly split into `.partNNNN` chunk assets, or
  - one `.bundle.zip` asset containing every source file (`bundle-assets` mode), also chunkable;
  - plus `_manifest.json` (authoritative index) and optionally `_cover.jpg`.

Two constants gate everything (`api.py`):

```python
STORAGE_FORMAT = "github-drive-archive"
METADATA_VERSION = 1
```

Storage modes (`storage.py`):

```python
STORAGE_MODE_FILE_ASSETS = "file-assets"
STORAGE_MODE_BUNDLE_ASSETS = "bundle-assets"
```

Upload modes (request-level, not stored): `UPLOAD_MODE_AUTO = "auto"`, `UPLOAD_MODE_FILES = "files"`,
`UPLOAD_MODE_BUNDLE = "bundle"`.

---

## 1. Release / archive identity

### 1.1 `archive_id`

Generated in `_prepare_upload_release` (`storage.py`):

```python
archive_id = uuid.uuid4().hex[:12].upper()
```

- **12 characters**, alphabet `0-9A-F` (uppercase hex), taken from the first 12 hex chars of a random UUID4.
- Stored **uppercase** in `archive_meta["archive_id"]` and in `_manifest.json`.

Kotlin equivalent:

```kotlin
val archiveId = UUID.randomUUID().toString().replace("-", "").take(12).uppercase()
```

### 1.2 Git tag

`api.py`:

```python
ARCHIVE_TAG_PREFIX = "github-drive-"

def archive_tag_for(archive_id: str) -> str:
    return f"{ARCHIVE_TAG_PREFIX}{archive_id.lower()}"
```

- Tag = `"github-drive-"` + `archive_id.lower()` → e.g. `github-drive-3f9a12bc44de`.
- Note the **case asymmetry**: id is uppercase in metadata, lowercase in the tag. Lookup by
  `archive_id` always goes through `archive_tag_for()`, so lowercase the id before building the tag.
- Total tag length is always `13 + 12 = 25` chars.

### 1.3 Release name (title)

`storage.py`:

```python
def _make_archive_title(source_name: str, total_items: int) -> str:
    safe_source = re.sub(r"\s+", " ", source_name).strip() or "archive"
    return f"GitHub Drive | {safe_source} | {total_items} items"
```

- Format literally: `GitHub Drive | {source_name} | {N} items` — spaces around each `|`.
- `source_name` has all whitespace runs (`\s+`) collapsed to a single space and is trimmed; empty → `"archive"`.
- Always the plural word `items`, even when `N == 1`.
- The title is recomputed on `append_to_archive` and `delete_archive_file` via
  `client.update_release(..., name=_make_archive_title(...))`.

### 1.4 Release body

`api.py`:

```python
ARCHIVE_MARKER = "GITHUB_DRIVE_ARCHIVE="

def encode_archive_body(metadata: Dict) -> str:
    payload = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True)
    return (
        "GitHub Drive archive. Do not edit the marker line below; it is parsed by the tool.\n\n"
        f"```\n{ARCHIVE_MARKER}{payload}\n```\n"
    )
```

Exact bytes (`\n` newlines, no `\r`):

```
GitHub Drive archive. Do not edit the marker line below; it is parsed by the tool.
<blank line>
```
GITHUB_DRIVE_ARCHIVE={"storage_format":"github-drive-archive",...}
```
```
(the fenced block uses three backticks; the file ends with a trailing newline)

JSON encoding of the body payload: **compact** — separators `,` and `:` with **no spaces**, and
`ensure_ascii=True` so every non-ASCII codepoint is `\uXXXX`-escaped. Kotlin must match both to be
byte-identical.

Decoder (`decode_archive_body`) is tolerant: it splits on lines, `strip()`s each, and takes the first
line that `startswith(ARCHIVE_MARKER)`, `json.loads`-ing the remainder. Returns `None` on parse failure.
A release **is** a github-drive archive iff this returns a dict.

### 1.5 Body metadata keys (`archive_meta`)

Written in `upload_archive` in this insertion order (JSON preserves it):

| key | type | meaning |
|---|---|---|
| `storage_format` | str | always `"github-drive-archive"` |
| `metadata_version` | int | always `1` |
| `created_at` | str | UTC ISO-8601, second precision, `+00:00` offset (see §1.7) |
| `source_name` | str | display name (folder or file name) |
| `source_type` | str | `"file"` or `"directory"` |
| `source_path` | str | absolute local path of the source on the uploading machine (`str(source)`) |
| `total_items` | int | number of source files (`len(entries)`) |
| `encrypted` | bool | archive-level encryption flag |
| `storage_mode` | str | `"file-assets"` or `"bundle-assets"` |
| `kinds` | obj | `{"image":n,"video":n,"audio":n,"document":n,"archive":n,"code":n,"other":n}` |
| `cover_asset_name` | str\|null | `"_cover.jpg"` if any entry is image/video, else `null` |
| `virtual_folders` | list[str] | **only present** on `create_empty_archive`, `append_to_archive`, `delete_archive_file` paths (see §2.6) |
| `archive_id` | str | 12 uppercase hex chars — **appended last** by `_prepare_upload_release` |

`create_empty_archive` inserts `virtual_folders` before `archive_id`, and sets
`source_type="directory"`, `total_items=0`, `encrypted=False`, all `kinds` zero, `cover_asset_name=None`,
`source_path = normalized_name` (not an absolute path).

`upload_browser_single_file` sets `source_type="file"`, `source_path = normalized_relative_path`
(the relative name, not an absolute path), and omits `virtual_folders`.

### 1.6 Draft / prerelease

`_create_release_idempotent`:

```python
return client.create_release(
    tag=tag, name=title, body=body,
    draft=False,
    prerelease=bool(private_release),
)
```

- `draft` is **always `false`**.
- `prerelease` mirrors the caller's `private_release` flag (the web UI's "private release" checkbox).
  There is no other use of prerelease; readers ignore it (it is only surfaced in listings).
- `target_commitish` is supported by `create_release` but **never passed** by the archive code.

### 1.7 `created_at` format

```python
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
```

→ `"2026-08-29T19:23:45+00:00"` — seconds precision, explicit `+00:00` offset, **not** a `Z` suffix.
Kotlin: `OffsetDateTime.now(ZoneOffset.UTC).truncatedTo(ChronoUnit.SECONDS).toString()` yields
`2026-08-29T19:23:45Z` — that is **wrong**; format manually as `...+00:00`.

### 1.8 Discovery / listing

`list_drive_archives(client)`:

1. `client.list_releases()` → `GET /repos/{owner}/{repo}/releases?per_page=100`, following `Link: <...>; rel="next"`.
2. For each release, `decode_archive_body(release["body"])`. Skip releases where it returns `None`.
   **The tag prefix is not used as the filter** — the marker line is.
3. Build a row:
   `release_id, tag, name (falls back to tag_name then ""), html_url, draft, prerelease,
   asset_count (len of release["assets"]), total_asset_bytes (sum of asset["size"]),
   created_at, updated_at, archive (the decoded metadata dict)`.
4. Sort: `key = archive["created_at"] or release["created_at"] or ""`, **reverse=True** (string sort on ISO timestamps).

`list_drive_archives_page(client, page, per_page=24)` is the same over one page:
`GET /repos/{owner}/{repo}/releases?per_page={per_page}&page={page}`; `has_more` = presence of a
`rel="next"` link. `per_page` is clamped to `1..100`.

Resolution by identity (`_resolve_release`), in precedence order:
`release_id` → `GET /releases/{id}`; else `tag` → `GET /releases/tags/{tag}`; else `archive_id` →
`GET /releases/tags/{archive_tag_for(archive_id)}`; else error.

---

## 2. `_manifest.json`

```python
MANIFEST_ASSET_NAME = "_manifest.json"
```

Uploaded as a release asset with `content_type="application/json"`, serialized as:

```python
json.dumps(manifest_payload, indent=2).encode("utf-8")
```

- `indent=2` → separators become `(",", ": ")` (Python's rule when `indent` is set), 2-space indent,
  newline-separated, **no trailing newline**.
- `ensure_ascii` defaults to `True` → non-ASCII escaped as `\uXXXX`.
- Kotlin must reproduce Python's `json.dumps(..., indent=2)` layout exactly for byte-identical output:
  `{\n  "key": value,\n  ...\n}`, arrays as `[\n    {...},\n    {...}\n  ]`, and **empty containers on one
  line** (`[]`, `{}`) — Python emits `"items": []` for an empty list, not `[\n]`.

### 2.1 Top-level schema (primary producer: `upload_archive`)

```python
manifest_payload = {
    "storage_format": STORAGE_FORMAT,   # "github-drive-archive"
    "metadata_version": METADATA_VERSION,  # 1
    "archive_id": archive_id,           # "3F9A12BC44DE"
    "created_at": created_at,           # ISO-8601 +00:00
    "source_name": source_name,         # str
    "source_type": source_type,         # "file" | "directory"
    "source_path": str(source),         # absolute local path
    "total_items": len(entries),        # int, count of SOURCE FILES
    "encrypted": bool(encrypt),         # bool
    "storage_mode": storage_mode,       # "file-assets" | "bundle-assets"
    "items": [asdict(item) for item in items],
}
```

`upload_browser_single_file` emits the **same key order** with `total_items = 1`, `source_type="file"`.

### 2.2 Per-item schema (`ArchiveItem`, `asdict` key order)

```python
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
```

| key | type | meaning |
|---|---|---|
| `order` | int | 0-based index of this entry in the sorted source-entry list. Sort key for the whole `items` array. In `append_to_archive` new orders continue from `max(existing order) + 1`. |
| `asset_name` | str | the **first part's** asset name (`parts[0].asset_name`). For single-asset entries this is the only asset. |
| `asset_id` | int | GitHub numeric asset id of `parts[0]`. |
| `relative_path` | str | path inside the archive, `/`-separated, no leading `/`. In `bundle-assets` mode this is the **bundle file name** (e.g. `MyFolder.bundle.zip`), not a user file. |
| `original_size` | int | size in bytes of the **plaintext, unchunked** source file (bundle: the zip's size). |
| `source_sha256` | str | lowercase hex SHA-256 of the plaintext source file. **Empty string `""`** when the entry was resumed/skipped (assets already existed) or when a manifest is rewritten by append/delete. |
| `encrypted` | bool | whether this entry's assets carry the GDRV envelope. |
| `content_type` | str | MIME type used for the asset upload. `"application/octet-stream"` whenever `encrypted`; otherwise `mimetypes.guess_type(source_path)` or `"application/octet-stream"`. |
| `parts` | list[obj] | chunk list, always ≥ 1 element. See §2.3. |
| `members` | list[obj] | non-empty **only** in `bundle-assets` mode. See §2.4. |

### 2.3 `parts[]` entry

Produced by `_upload_entry`:

```python
parts_meta.append({
    "order": chunk_index,     # 0-based chunk index
    "asset_name": asset_name, # exact GitHub asset name
    "asset_id": asset["id"],  # int
    "size": int(asset.get("size") or os.path.getsize(upload_path)),  # ON-THE-WIRE bytes (post-encryption)
})
```

- `size` is the **uploaded asset size**, i.e. includes the 33-byte GDRV header when encrypted. It is *not*
  the plaintext chunk length.
- Reassembly order is **`parts` sorted ascending by `order`** — readers always do
  `sorted(item["parts"], key=lambda p: p["order"])`, never trusting array order.

**AMBIGUITY / gotcha:** when a manifest is *rewritten* by `append_to_archive` or `delete_archive_file`,
parts come from `_build_download_items`, which constructs them with a **different key order**:

```python
parts.append({"order": ..., "asset_id": ..., "asset_name": ..., "size": ...})
```

So `asset_id` precedes `asset_name` in rewritten manifests. Values are identical; only JSON key order
differs. Byte-identical output therefore depends on **which code path wrote the manifest**. If your
Kotlin port needs byte-identical manifests, replicate the ordering per operation:
fresh upload → `order, asset_name, asset_id, size`; append/delete rewrite → `order, asset_id, asset_name, size`.

### 2.4 `members[]` entry (bundle mode only)

Produced in `_create_bundle_archive`:

```python
members.append({
    "relative_path": relative_path,                 # path inside the zip, '/'-separated
    "original_size": int(entry["size_bytes"]),      # plaintext size
    "source_sha256": _sha256_file(source_path),     # lowercase hex
    "content_type": guessed or "application/octet-stream",
})
```

Member order in the array = the order files were written into the zip = the sorted entry order (§2.5).
Readers look members up **by name** in the zip central directory, so member array order is not
load-bearing for extraction, only for progress reporting.

### 2.5 Ordering / folder structure encoding

`collect_file_entries` (`storage.py`) defines canonical entry order:

```python
pattern = "**/*" if recursive else "*"
for candidate in sorted(source.glob(pattern)):
    if not candidate.is_file():
        continue
    entries.append({
        "source_path": str(candidate),
        "relative_path": str(candidate.relative_to(source)),
        "size_bytes": candidate.stat().st_size,
    })
```

- Order = `sorted()` over `Path` objects from `glob("**/*")`. `PurePath.__lt__` compares the
  case-normalized **string form of the whole path** (on POSIX: plain `str(path)`), so the effective order is
  a codepoint-wise lexicographic sort of the full `/`-separated paths — note `-` (0x2D) < `.` (0x2E) <
  `/` (0x2F) < digits < uppercase < `_` (0x5F) < lowercase. In particular a path sorts *before* its own
  children's separator boundary in ways a naive per-segment sort would not reproduce: sort the whole
  string, not segment by segment.
- Directories are skipped; only files become entries. **Empty directories are lost** unless recorded as
  virtual folders (§2.6).
- `relative_path` uses `str(Path.relative_to(...))` → on POSIX this yields `/` separators. On Windows it
  would yield `\`, which the rest of the code normalizes away in several places but not here.
  **Android must always emit `/`.**
- Single-file source: one entry whose `relative_path` is just `source.name`.
- `entries` is empty → `ValueError("No files were found in ...")`.

Web-upload paths are normalized by `_safe_upload_relative_path` (`webapp.py`):
backslashes → `/`, NUL stripped, leading `/` stripped, `.` segments dropped, `..` rejected
(`ValueError`), control chars `[\x00-\x1f\x7f]` rejected, segments rejoined with `/`.

Download safety (`_resolve_destination_path`): the resolved target must stay under the destination root,
else `RuntimeError("Refusing to write outside destination: ...")`.

### 2.6 Virtual (empty) folders — **not in the manifest**

`archive_meta["virtual_folders"]` lives **only in the release body**, never in `_manifest.json`
(`_manifest_payload_from_items` does not include it). Semantics (`_normalize_virtual_folders`):

- Every entry is normalized (`_normalize_folder_path`: `\`→`/`, trim, strip leading/trailing `/`, drop
  empty and `.` segments, **reject `..`**).
- For each folder, **all ancestors** are added (`_folder_ancestors("a/b/c")` → `["a","a/b","a/b/c"]`).
- For each file path containing `/`, all ancestors of its parent are added.
- Result is `sorted(set(...))` — plain lexicographic.

An Android reader must merge `virtual_folders` from the body with the manifest's file paths to render the
full tree (this is what `_flatten_archive_entries` does, emitting synthetic entries with `kind:"folder"`,
`original_size: 0`, `content_type: ""`, `previewable: false`).

### 2.7 The other manifest producer (append / delete / empty-archive)

`_manifest_payload_from_items` — **different key set and order**:

```python
{
    "storage_format": ..., "metadata_version": ...,
    "archive_id": ..., "source_name": ..., "source_path": ...,
    "created_at": ..., "total_items": len(items),
    "encrypted": ..., "storage_mode": ...,
    "items": [...],
}
```

Differences vs §2.1 — both are real and observable on the wire:

1. **`source_type` is absent.**
2. `source_name`/`source_path` come **before** `created_at` (reversed relative to §2.1).
3. `total_items` = `len(items)` (manifest item count) rather than `len(entries)` (source-file count).
   In `bundle-assets` mode the two disagree: a fresh bundled upload writes `total_items` = number of
   source files while `items` has length 1; a rewritten manifest would write `1`.
   (`bundle-assets` archives reject append/delete, so this specific collision does not occur in practice,
   but the `total_items` semantics differ by producer regardless.)

**AMBIGUITY:** the code never states which of the two shapes is canonical. A reader must treat
`source_type` as optional and must not rely on `total_items` matching `len(items)`.

### 2.8 Reader precedence

`_build_download_items`:

1. If `_manifest.json` exists, download it (`download_asset_bytes(..., use_cache=True)`) and `json.loads`.
   On `GitHubError`/`JSONDecodeError`/`UnicodeDecodeError` → `manifest = None`.
2. `selected_meta = manifest or archive_meta` — the **manifest wins** over the release body for
   `encrypted` and `storage_mode`.
3. Manifest items are joined to live assets **by `parts[].asset_name`**; parts whose asset name is missing
   from the release are silently dropped.
4. **Legacy fallback inside an item:** if no `parts` survive, synthesize one part from the item's
   top-level `asset_name`. If that asset is missing too, the item is dropped entirely.
5. **No-manifest fallback:** iterate all assets except `_manifest.json` and decode names heuristically
   (`_decode_asset_name`, §3.5). Note this fallback does **not** exclude `_cover.jpg`, so a coverless-manifest
   archive would treat `_cover.jpg` as a file entry.
6. `items.sort(key=order)`.
7. `progress_total` = `sum(len(members))` in bundle mode, else `len(items)`.

---

## 3. Asset naming

Sanitizer (`storage.py`):

```python
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

def _sanitize_for_asset_name(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value).strip("-.")
    return cleaned or "file"
```

- Every **run** of characters outside `[A-Za-z0-9._-]` collapses to a **single** `-`.
- Then leading/trailing `-` and `.` characters are stripped (`str.strip("-.")` strips any mix of both).
- Empty result → `"file"`.

### 3.1 Plain (single-asset) file

```python
def _asset_name_for(order: int, relative_path: str, encrypted: bool) -> str:
    flat = relative_path.replace("/", "__").replace("\\", "__")
    safe = _sanitize_for_asset_name(flat)[:180]
    suffix = ENCRYPTED_SUFFIX if encrypted else ""   # ".enc"
    return f"{order:04d}-{safe}{suffix}"
```

Format: `NNNN-<safe>[.enc]`

- `NNNN` = `order` zero-padded to **4 digits, 0-based**. Orders ≥ 10000 produce 5+ digits (Python's `:04d`
  is a minimum width, not a truncation).
- Path separators (`/` and `\`) become **double underscore `__`** *before* sanitization (`_` is in the safe
  set, so `__` survives).
- Truncated to **180 chars** *after* sanitization (so a trailing `-` or `.` can survive truncation).
- `.enc` suffix appended only when encrypted (`ENCRYPTED_SUFFIX = ".enc"`).

Example: order 7, `photos/2024/IMG 001.jpg`, encrypted →
`0007-photos__2024__IMG-001.jpg.enc`

### 3.2 Chunked (multi-part) file

```python
def _part_asset_name_for(order, relative_path, chunk_index, encrypted) -> str:
    flat = relative_path.replace("/", "__").replace("\\", "__")
    safe = _sanitize_for_asset_name(flat)[:160]
    suffix = ENCRYPTED_SUFFIX if encrypted else ""
    return f"{order:04d}-{safe}.part{chunk_index:04d}{suffix}"
```

Format: `NNNN-<safe>.partKKKK[.enc]`

- Truncation is **160** chars here (vs 180 for single assets) to leave room for `.partKKKK`.
- `KKKK` = `chunk_index` zero-padded to **4 digits, 0-based** (`part0000` is the first chunk).
- The literal token is lowercase `.part`.
- A file that fits in one chunk **never** gets a `.partKKKK` name — the single-asset form (§3.1) is used,
  which is why old single-asset archives stay readable.

Example: order 0, `bigvideo.mkv`, 3 chunks, no encryption →
`0000-bigvideo.mkv.part0000`, `0000-bigvideo.mkv.part0001`, `0000-bigvideo.mkv.part0002`

### 3.3 Bundled file

```python
BUNDLE_ARCHIVE_SUFFIX = ".bundle.zip"

def _bundle_relative_name(source_name: str) -> str:
    safe = _sanitize_for_asset_name(source_name)[:120]
    return f"{safe}{BUNDLE_ARCHIVE_SUFFIX}"
```

- Bundle's `relative_path` = `<sanitized source folder name, ≤120 chars>.bundle.zip`.
- The bundle is then uploaded through the **same** naming functions with `order = 0`:
  - single asset: `0000-<sanitized bundle name>[.enc]` → e.g. `0000-MyPhotos.bundle.zip`
  - chunked: `0000-<sanitized bundle name>.partKKKK[.enc]`
- Individual bundle members get **no assets of their own**.

### 3.4 Reserved asset names

- `_manifest.json` (`MANIFEST_ASSET_NAME`)
- `_cover.jpg` (`COVER_ASSET_NAME`, defined identically in both `thumbnails.py` and `storage.py`)

Both begin with `_`, which the `NNNN-` naming scheme can never produce, so there is no collision.

### 3.5 Reassembly by a reader

Preferred path — **use the manifest**:

1. For each item, sort `parts` by `order` ascending.
2. Download each part by `asset_id`.
3. If `item.encrypted` (or the archive-level `encrypted` flag) → GDRV-decrypt each part **independently**
   (§6); each part has its own header and nonce.
4. Concatenate the decrypted/raw parts in `order` sequence into `relative_path`.
5. In bundle mode, the concatenated result is a ZIP; extract members by `member.relative_path`
   (`RuntimeError` if a listed member is absent from the zip).

Fallback without a manifest (`_decode_asset_name`) — best-effort only:

```python
name = asset_name
encrypted = name.endswith(".enc") or archive_encrypted
if name.endswith(".enc"):
    name = name[:-len(".enc")]
order = 0
if "-" in name:
    head, rest = name.split("-", 1)
    if head.isdigit():
        order = int(head)
        name = rest
relative_path = name.replace("__", "/")
```

**AMBIGUITY:** this fallback does **not** understand `.partKKKK` — each chunk would be treated as an
independent file, and `.partKKKK` would remain in the reconstructed name. Chunked archives are only
correctly recoverable **with** the manifest. Similarly, `-` reintroduced by the sanitizer is never
reversed, so sanitized names are lossy by design; the manifest is the only source of true paths.

---

## 4. Bundling ("auto-bundle mode")

### 4.1 Trigger

```python
BUNDLE_FILE_COUNT_THRESHOLD = 256
BUNDLE_TINY_FILE_THRESHOLD = 8 * 1024 * 1024   # 8_388_608 bytes

def _choose_storage_mode(entries, upload_mode):
    if upload_mode == "files":  return STORAGE_MODE_FILE_ASSETS
    if upload_mode == "bundle": return STORAGE_MODE_BUNDLE_ASSETS
    return STORAGE_MODE_BUNDLE_ASSETS if _should_bundle_entries(entries) else STORAGE_MODE_FILE_ASSETS

def _should_bundle_entries(entries):
    if len(entries) <= 1:
        return False
    if len(entries) >= 1000:
        return True
    total_bytes = sum(int(entry["size_bytes"]) for entry in entries)
    average = total_bytes / max(len(entries), 1)
    return len(entries) >= BUNDLE_FILE_COUNT_THRESHOLD and average <= BUNDLE_TINY_FILE_THRESHOLD
```

Decision table for `upload_mode="auto"`:

| condition | result |
|---|---|
| ≤ 1 file | `file-assets` |
| ≥ 1000 files | `bundle-assets` (regardless of size) |
| 256 ≤ files < 1000 **and** mean size ≤ 8 MiB | `bundle-assets` |
| otherwise | `file-assets` |

`average` is a **float** division (`total_bytes / len`), compared with `<=`. `upload_mode` is validated by
`_normalize_upload_mode` (lowercased, must be one of `auto`/`files`/`bundle`, else `RuntimeError`).

### 4.2 Container format

`_create_bundle_archive`:

```python
with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
    for entry in entries:
        archive.write(entry["source_path"], arcname=entry["relative_path"])
```

- **ZIP** (not tar), **DEFLATE** compression, **Zip64 enabled**.
- Compression level: Python's `zipfile` default for `ZIP_DEFLATED` (zlib default level, i.e. **6**);
  `compresslevel` is not passed. Kotlin: `Deflater.DEFAULT_COMPRESSION` / `ZipOutputStream` default.
- `arcname` = the entry's `relative_path` verbatim, `/`-separated, no leading `/`.
- Member write order = the canonical sorted entry order (§2.5).
- No password, no per-member encryption; encryption (if any) wraps the **entire finished zip**.

**AMBIGUITY (important for "byte-identical"):** a ZIP produced by Python's `zipfile.write()` embeds the
source file's mtime (DOS date/time, 2-second granularity), the source's Unix permission bits in the
external attributes, `create_system=3` (Unix), version-made-by, and Zip64/extra fields at Python's
discretion. **Byte-for-byte reproducibility of the bundle across Python and Kotlin is not achievable
from the code alone.** Interop requirement is therefore: Android must produce a *readable* zip whose
members match the manifest's `members[].relative_path`, and must read Python's zips by member name.
Do not attempt byte-equality on the bundle container itself.

### 4.3 Manifest recording

One `ArchiveItem` with:

- `order = 0`
- `relative_path = "<sanitized-source-name>.bundle.zip"`
- `original_size = os.path.getsize(bundle_path)` (the zip size)
- `source_sha256` = SHA-256 of the **zip file** (or `""` if resumed)
- `content_type` = `"application/zip"` when unencrypted (from `mimetypes.guess_type` on the `.zip` path),
  `"application/octet-stream"` when encrypted
- `parts` = the chunk list for the zip
- `members` = one object per real user file (§2.4)

The manifest's `storage_mode` is `"bundle-assets"`; readers branch on it
(`_flatten_archive_entries`, `_download_bundle_item`, `_find_archive_entry`).

### 4.4 Restrictions on bundle archives

- `append_to_archive` → `RuntimeError("Adding files into existing folders is unavailable for bundled archives.")`
- `delete_archive_file` → `RuntimeError("Individual file delete is unavailable for bundled archives.")`
- `create_archive_folder` → `RuntimeError("Empty folders are unavailable for bundled archives.")`
- `list_archive_contents` reports `"supports_file_delete": storage_mode == "file-assets"`.

Single-file fetch from a bundle downloads and reassembles the **entire** bundle, then extracts one member
(`fetch_archive_file_to_disk`).

---

## 5. Chunking

```python
DEFAULT_CHUNK_BYTES = 1_900_000_000  # ~1.9 GB; sits comfortably under GitHub's 2 GB asset cap.
COPY_BUFFER = 4 * 1024 * 1024        # 4 MiB streaming buffer

def _chunk_size_bytes() -> int:
    raw = os.environ.get("GITHUB_DRIVE_CHUNK_BYTES")
    ...  # int(raw) if > 0 else DEFAULT_CHUNK_BYTES; non-int → DEFAULT

def _split_threshold(encrypt: bool) -> int:
    base = _chunk_size_bytes()
    overhead = crypto.HEADER_LEN if encrypt else 0   # 33
    return max(1, base - overhead)
```

- Chunk budget: **1 900 000 000 bytes** (decimal, not 1.9 GiB), overridable by `GITHUB_DRIVE_CHUNK_BYTES`.
- Plaintext-per-chunk threshold: `1_900_000_000` unencrypted, `1_899_999_967` encrypted
  (`1_900_000_000 - 33`) so the uploaded asset still fits the budget.

Planner (`_plan_entry_assets`):

```python
if file_size <= threshold:
    return [{"chunk_index": 0, "chunk_offset": 0, "chunk_length": file_size,
             "asset_name": _asset_name_for(order, relative_path, encrypt)}]
# else
while remaining > 0:
    length = min(threshold, remaining)
    plan.append({"chunk_index": chunk_index, "chunk_offset": offset,
                 "chunk_length": length,
                 "asset_name": _part_asset_name_for(order, relative_path, chunk_index, encrypt)})
    offset += length; remaining -= length; chunk_index += 1
```

- `file_size <= threshold` (inclusive) → **single** asset, plain name.
- Otherwise: chunks of exactly `threshold` plaintext bytes, last chunk = remainder.
- A **0-byte** file yields a single plan entry with `chunk_length = 0` (a 0-byte asset; GitHub accepts it).
- Chunks are sliced from the plaintext, then **each chunk is encrypted separately** (its own GDRV header
  and nonce) — encryption is never applied across a chunk boundary.

Reassembly order: strictly ascending `parts[].order` (== `chunk_index`), each part decrypted before
appending. From `_materialize_download_parts` / `_download_item`:

```python
parts = sorted(item.get("parts") or [], key=lambda part: part["order"])
...
with open(output_path, "wb") as out:
    for index, part in enumerate(parts):
        download -> (decrypt) -> append 4 MiB at a time -> unlink chunk
```

Single-part items take a "download then move/decrypt directly to target" fast path — behaviourally
identical output.

---

## 6. Encryption (`crypto.py`) — byte-exact

### 6.1 On-the-wire layout of an encrypted asset

Module docstring and code agree:

```python
MAGIC = b"GDRV"        # 0x47 0x44 0x52 0x56
VERSION = 0x01
NONCE_BYTES = 12
TAG_BYTES = 16
HEADER_LEN = len(MAGIC) + 1 + NONCE_BYTES + TAG_BYTES   # 4 + 1 + 12 + 16 = 33
```

```
offset  len   field
------  ----  ---------------------------------------------
 0      4     magic       = 'G','D','R','V'  (0x47445256)
 4      1     version     = 0x01
 5      12    nonce/IV    (os.urandom, per asset/chunk)
17      16    GCM tag
33      *     ciphertext  (same length as plaintext)
------  ----
total = 33 + plaintext_length
```

**The tag precedes the ciphertext.** This is the single most important deviation from the common
`nonce || ciphertext || tag` convention, and from Java/Kotlin's `Cipher` with `GCMParameterSpec`, which
expects the tag **appended** to the ciphertext. On Android you must splice:

```kotlin
// encrypt
val cipher = Cipher.getInstance("AES/GCM/NoPadding")
val nonce = ByteArray(12).also { SecureRandom().nextBytes(it) }
cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
val ctPlusTag = cipher.doFinal(plaintext)          // ciphertext || tag
val ct  = ctPlusTag.copyOfRange(0, ctPlusTag.size - 16)
val tag = ctPlusTag.copyOfRange(ctPlusTag.size - 16, ctPlusTag.size)
out.write("GDRV".toByteArray(Charsets.US_ASCII)); out.write(0x01)
out.write(nonce); out.write(tag); out.write(ct)

// decrypt: read 33-byte header, verify magic + version, then
cipher.init(Cipher.DECRYPT_MODE, ..., GCMParameterSpec(128, nonce))
val plain = cipher.doFinal(ct + tag)               // re-append the tag
```

Other exact facts:

- **AAD: none.** `AES.new(key, AES.MODE_GCM, nonce=nonce)` with no `update()` call → zero-length AAD.
- **Tag length: 16 bytes (128 bits).** PyCryptodome's GCM default `mac_len=16`. Kotlin: `GCMParameterSpec(128, nonce)`.
- **Nonce: 12 bytes** from `os.urandom` → standard GCM `J0 = IV || 0x00000001`, no GHASH derivation.
  Fresh per encrypted asset/chunk. Never reused; nothing derives it deterministically.
- **No salt is stored in the file, and no per-file KDF exists.** See §6.2.
- Ciphertext length == plaintext length (CTR mode).
- Decrypt validation (`decrypt_file`): reads exactly 33 bytes; errors if short **or** magic mismatch
  (`"{path} is not a github-drive encrypted file."`); errors if `version != 0x01`
  (`"Unsupported encryption version {v} in {path}"`); then `decrypt_and_verify` (raises on bad tag).
- Whole-file, in-memory: `plaintext = src.read()` and one `encrypt_and_digest` call. Per-chunk splitting
  (§5) is what bounds memory.
- Key length validation (`_validate_key`): must be `bytes`/`bytearray` of length **16, 24, or 32**, else
  `RuntimeError("AES key must be 16, 24, or 32 bytes.")`. So AES-128/192/256-GCM are all valid on the wire;
  the header does **not** record which — the reader must already know the key.

The asset also carries the naming marker `.enc` (`ENCRYPTED_SUFFIX = ".enc"`) and, when encrypted,
`content_type = "application/octet-stream"` regardless of the source type.

### 6.2 Key derivation — **there is no passphrase KDF**

**AMBIGUITY / premise correction:** the question asks for "KDF, iterations, salt source and length" for
deriving the AES key from a passphrase. **No such KDF exists in this codebase.** There is no PBKDF2,
scrypt, or Argon2 applied to an archive passphrase, no salt is generated for archive encryption, and the
GDRV header has no salt field. The scrypt/PBKDF2 constants in `users.py`
(`SCRYPT_N = 2**14, SCRYPT_R = 8, SCRYPT_P = 1, SCRYPT_DKLEN = 32, PBKDF2_ITERATIONS = 600_000,
SALT_BYTES = 16`) are for **login password hashing only** and never touch archive bytes.

The AES key is resolved by one of four routes:

**(a) CLI `--key <passphrase>`** (`main.py:_resolve_encode_key`) — a raw byte pad, *not* a KDF:

```python
key_bytes = raw_passphrase.encode("utf-8")
if len(key_bytes) < 16:
    key_bytes = key_bytes.ljust(16, b"0")   # pads with ASCII '0' (0x30)
return key_bytes[:16]
```
→ always exactly **16 bytes**: UTF-8 of the passphrase, right-padded with `'0'` (0x30) to 16, then
truncated to 16. Zero stretching, zero salt.

**(b) `GITHUB_DRIVE_ENCRYPTION_KEY`** (`auth_manager._decode_encryption_key`) — raw key material:
try `bytes.fromhex(value)` first; if that fails or the length isn't 16/24/32, try base64 with padding
repaired (`value + "=" * (-len(value) % 4)`), standard alphabet then URL-safe. Must decode to 16/24/32
bytes or `RuntimeError`.

**(c) Legacy single-user fallback** (`auth_manager.derive_encode_key`) — used only when (b) is unset and
`GITHUB_DRIVE_SESSION_SECRET` is set:

```python
digest = hmac.new(server_secret.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256).digest()
return digest[:16]
```
where `user_id = (GITHUB_DRIVE_USER_ID or "default").strip() or "default"`. → **16 bytes** (AES-128).

**(d) Multi-user web app** (`users.derive_user_archive_key`) — the path the hosted web app actually uses
(`webapp._user_encode_key` → `users.derive_user_archive_key(username)`):

```python
base_key = get_encryption_key()   # (b) then (c)
return hmac.new(base_key,
                f"archive:{normalize_username(username)}".encode("utf-8"),
                hashlib.sha256).digest()[:16]
```

- HMAC-SHA256, key = the raw base key bytes, message = the ASCII string `archive:` + the **normalized**
  username (`(username or "").strip().lower()`), digest **truncated to the first 16 bytes** → AES-128.
- Kotlin: `Mac.getInstance("HmacSHA256")` with `SecretKeySpec(baseKey, "HmacSHA256")`,
  `doFinal("archive:$username".toByteArray(UTF_8)).copyOf(16)`.

Precedence (`auth_manager.get_encryption_key`): `GITHUB_DRIVE_ENCRYPTION_KEY` → else
`GITHUB_DRIVE_SESSION_SECRET`-derived legacy key → else `RuntimeError`. The CLI `--key` overrides both
at the call site.

Unrelated but adjacent (do **not** confuse with archive bytes): `users._encrypt_at_rest` stores GitHub
PATs as `base64(nonce(12) || tag(16) || ciphertext)` — **no `GDRV` magic, no version byte** — under a
different key `HMAC-SHA256(GITHUB_DRIVE_SESSION_SECRET, "at-rest:<username>")[:16]`. Different layout,
different key, different purpose.

### 6.3 Consistency rules enforced

- `upload_archive` / `append_to_archive`: `encrypt=True` without `encode_key` → `RuntimeError("encrypt=True requires encode_key.")`, then `crypto._validate_key(encode_key)`.
- `append_to_archive`: `bool(existing_encrypted) != bool(encrypt)` → `RuntimeError("Upload encryption setting does not match this archive.")`.
- Resume: `_prepare_upload_release` rejects a mismatch of `encrypted` and of `storage_mode`.
- `download_archive`: encrypted archive without key → `RuntimeError("This archive is encrypted; an encode_key is required to download.")`.
- Per-item override: `is_encrypted = bool(item.get("encrypted") or encrypted)` — an item flagged encrypted
  is decrypted even if the archive flag is false.

---

## 7. Limits

### 7.1 `limits.py` — **contains no size or count limits**

**AMBIGUITY / premise correction:** `limits.py` is a 40-line rate-limiting helper, not a size-limit table.
It exports exactly:

```python
class RateLimitExceeded(RuntimeError): ...

def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw: return default
    try: value = int(raw)
    except ValueError: return default
    return value if value >= 0 else default          # NOTE: 0 is accepted; negatives fall back

def check_rate_limit(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    # no-op when limit <= 0 or window_seconds <= 0
    # in-process sliding window over a deque of timestamps per (bucket, key)
```

The buckets are **in-process only** (`_BUCKETS: Dict[Tuple[str,str], Deque[float]]` guarded by a
`threading.Lock`) — they do not survive a restart and are not shared across processes/instances.

### 7.2 Actual limits, env var names, defaults

Size / count limits (`webapp.py`):

| env var | default | meaning |
|---|---|---|
| `GITHUB_DRIVE_MAX_UPLOAD_BYTES` | `5 * 1024**3` = 5 GiB (`DEFAULT_MAX_UPLOAD_BYTES`) | Flask `MAX_CONTENT_LENGTH`-style request cap. Parsed with `int()`; must be `> 0` else default. |
| `GITHUB_DRIVE_USER_MAX_UPLOAD_BYTES` | `2 * 1024**3` = 2 GiB (`DEFAULT_USER_UPLOAD_BYTES`) | Per-browser-upload cap; enforced both on the single-file fast path and cumulatively while staging. HTTP 413 on breach. |
| `GITHUB_DRIVE_MAX_FILES_PER_UPLOAD` | `5000` | Max files in one multi-file browser upload. HTTP 413. |
| `GITHUB_DRIVE_COVER_SOURCE_MAX_BYTES` | `50 * 1024 * 1024` = 50 MiB | Source image assets bigger than this skip lazy server-side cover generation. |
| `GITHUB_DRIVE_STORAGE_LIMIT_BYTES` | unset → `None` (no limit) | Optional overall storage cap; `int()`, must be `> 0`. |
| `GITHUB_DRIVE_ARCHIVES_PAGE_SIZE` | `24`, clamped to `1..100` | Archives per listing page. |
| `GITHUB_DRIVE_CHUNK_BYTES` | `1_900_000_000` (`DEFAULT_CHUNK_BYTES`, `storage.py`) | Per-asset chunk budget. Must be `> 0`. |

Rate / concurrency limits (`webapp.py`, all through `limits.env_int`):

| env var | default | meaning |
|---|---|---|
| `GITHUB_DRIVE_AUTH_RATE_LIMIT` | `20` | Auth attempts per window, keyed by client IP (`X-Forwarded-For` first hop, else `remote_addr`, else `"unknown"`). |
| `GITHUB_DRIVE_AUTH_RATE_WINDOW_SECONDS` | `15 * 60` = 900 | Auth window. |
| `GITHUB_DRIVE_USER_ACTION_RATE_LIMIT` | `60` | User actions per window, keyed `"{user_id}:{client_ip}"`. |
| `GITHUB_DRIVE_USER_ACTION_RATE_WINDOW_SECONDS` | `60` | User-action window. |
| `GITHUB_DRIVE_MAX_ACTIVE_TASKS_PER_USER` | `3` | Concurrent queued+running tasks per user. |
| `GITHUB_DRIVE_MAX_ACTIVE_UPLOADS_PER_USER` | `3` | Per-type cap (name built as `GITHUB_DRIVE_MAX_ACTIVE_{TYPE}S_PER_USER`). |
| `GITHUB_DRIVE_MAX_ACTIVE_DOWNLOADS_PER_USER` | `2` | Per-type cap for downloads. |
| `GITHUB_DRIVE_MAX_ACTIVE_TASKS_GLOBAL` | `4` | Process-wide concurrent runners. |

Cache TTLs (`api.py`, via `_env_float` / `_env_int`; negative → default):

| env var | default |
|---|---|
| `GITHUB_DRIVE_RELEASES_CACHE_TTL_SECONDS` | `30.0` |
| `GITHUB_DRIVE_RELEASE_CACHE_TTL_SECONDS` | `30.0` |
| `GITHUB_DRIVE_RELEASE_ASSETS_CACHE_TTL_SECONDS` | `30.0` |
| `GITHUB_DRIVE_ASSET_BYTES_CACHE_TTL_SECONDS` | `600.0` |
| `GITHUB_DRIVE_ASSET_BYTES_CACHE_MAX_BYTES` | `2 * 1024 * 1024` (2 MiB) |

Other behavioural env vars: `GITHUB_DRIVE_ENCRYPTION_KEY`, `GITHUB_DRIVE_SESSION_SECRET`,
`GITHUB_DRIVE_USER_ID` (default `"default"`), `GITHUB_DRIVE_ENCRYPT`, `GITHUB_DRIVE_ALLOW_SIGNUP`
(default `"true"`), `GITHUB_DRIVE_ADMIN_USERS`, `GITHUB_DRIVE_BASIC_AUTH`,
`GITHUB_DRIVE_FORCE_SECURE_COOKIES`, `GITHUB_DRIVE_DATABASE_URL`/`DATABASE_URL`.

Implicit external limits the format is designed around: GitHub's **2 GB per release asset** (hence the
1.9 GB chunk budget) and **5 000 authenticated REST requests/hour** (hence auto-bundling).

---

## 8. Thumbnails / covers

```python
COVER_ASSET_NAME = "_cover.jpg"      # thumbnails.py, and duplicated in storage.py
```

### 8.1 Storage

- Uploaded as a normal release asset named exactly `_cover.jpg`, `content_type="image/jpeg"`, via
  `client.upload_asset_bytes(...)`.
- The release body metadata carries `"cover_asset_name": "_cover.jpg"` when any entry is an image or video
  (`_is_visual_path`), else `null`. **This field is advisory** — readers look the asset up by name.
- `_cover.jpg` is **not** listed in `_manifest.json` `items` and is explicitly skipped by
  `thumbnails.first_image_asset` / `first_visual_asset`.

### 8.2 How the cover is chosen

Three producers, in the order they occur:

1. **Browser-side (primary path, `static/app.js`)** — `pickCoverFile(group)` picks the **first entry**
   whose extension is in `COVER_IMAGE_EXTENSIONS = {"jpg","jpeg","png","gif","webp","bmp"}` (client-side
   set — note it excludes `tif/tiff` and all video). Rendered with
   `COVER_TARGET_SIZE = 480`, `COVER_JPEG_QUALITY = 0.82`, center square crop
   (`side = min(w,h)`, `sx = (w-side)/2`, `sy = (h-side)/2`), preferring
   `createImageBitmap(file, {resizeWidth: 960, resizeHeight: 960, resizeQuality: "high"})`.
   Posted as the multipart field `cover_blob` with filename `_cover.jpg`, then uploaded verbatim by
   `_maybe_attach_cover_blob` / `_maybe_attach_cover_from_path`. Skipped entirely when appending
   (`if (!group.appendTag)`).
2. **Server-side lazy fallback (`GET /api/archives/<release_id>/cover`)** — when no `_cover.jpg` asset
   exists: `thumbnails.first_image_asset(assets)` picks the first asset (excluding `_cover.jpg` and
   `_manifest.json`) whose extension is in `IMAGE_EXTENSIONS`; if its `size` exceeds
   `GITHUB_DRIVE_COVER_SOURCE_MAX_BYTES` (50 MiB) → 404. Otherwise download, `make_cover_jpeg`, upload as
   `_cover.jpg` (best-effort), and return the bytes with `Cache-Control: private, max-age=600`.
   **Videos are deliberately excluded from this lazy path.**
3. **Library helpers** (`make_cover_for_path`, used by CLI-ish flows): images → `make_cover_jpeg`;
   videos (`VIDEO_EXTENSIONS`) → `make_video_cover_jpeg` via ffmpeg; anything else → `None`.

`thumbnails.first_visual_entry(entries)` (first image **or** video entry) is what decides
`cover_asset_name` in the metadata — so metadata can claim a cover exists for a video-only archive even
though the lazy server path will never generate one. Missing cover ⇒ the client shows a generic icon.

### 8.3 Cover image format

- **JPEG**, square, default **480 × 480** (`size: int = 480`).
- Python path (`make_cover_jpeg`): `Image.draft("RGB", (size*4, size*4))` decode hint →
  `ImageOps.exif_transpose(img).convert("RGB")` → center square crop → `thumbnail((480,480), LANCZOS)` →
  `save(format="JPEG", quality=80, optimize=True)`.
- Browser path: quality **0.82**, canvas `drawImage` resample, **no EXIF transpose**.
- ffmpeg video path: `-ss 0.5 -frames:v 1 -vf "thumbnail,scale=480:480:force_original_aspect_ratio=increase,crop=480:480"`.

**AMBIGUITY:** the three producers emit visibly different JPEGs (quality 80 vs 0.82, LANCZOS vs canvas,
EXIF handling). **Cover bytes are explicitly not part of the interoperable format** — every reader just
fetches `_cover.jpg` and displays it. Android should generate a 480×480 center-cropped JPEG and not
attempt byte-equality.

### 8.4 Classification (`kinds`)

`classify_extension(ext)` (lowercased suffix incl. the dot), first match wins in this order:

- `image`: `.jpg .jpeg .png .gif .webp .bmp .tif .tiff`
- `video`: `.mp4 .mov .mkv .webm .avi .m4v .mts .m2ts .wmv .flv`
- `audio`: `.mp3 .flac .wav .aac .ogg .opus .m4a`
- `document`: `.pdf .docx .doc .xlsx .xls .pptx .ppt .txt .md .csv .rtf .odt`
- `archive`: `.zip .tar .gz .tgz .bz2 .7z .rar`
- `code`: `.py .js .ts .tsx .jsx .java .c .cc .cpp .h .hpp .go .rs .rb .php .sh .html .css .json .yaml .yml .toml`
- else `other`

`classify_entries` returns the count dict with **all seven keys always present** (zeros included) — that
exact key order (`image, video, audio, document, archive, code, other`) is what lands in the release body.

`preview_thumb_supported(ext)` = image or video.

---

## 9. Resume semantics

### 9.1 Choosing the release to resume into

`upload_archive(..., resume_release_id=?, resume_tag=?, resume_archive_id=?)` →
`_prepare_upload_release`. If **any** of the three is non-`None`/non-empty:

1. `_resolve_release` (id → tag → archive_id precedence).
2. Body must decode → else `RuntimeError("Release {tag} is not a github-drive archive.")`.
3. `resume_meta["source_name"] != source_name` → `RuntimeError("Resume target {tag} belongs to {other!r}, not {source_name!r}.")`
4. `bool(resume_meta["encrypted"]) != bool(archive_meta["encrypted"])` → `RuntimeError("Resume target encryption setting does not match this upload.")`
5. `(resume_meta.get("storage_mode") or "file-assets") != archive_meta["storage_mode"]` → `RuntimeError("Resume target storage mode does not match this upload mode.")`
6. **Reuse** `archive_id` and `created_at` from the existing release (falling back to freshly generated
   values if absent). The tag is therefore unchanged.

Otherwise a new `archive_id` is minted and the release created.

### 9.2 What is skipped

The skip decision is made **purely by asset name presence** — no size check, no checksum check.

```python
existing_assets = {asset["name"]: asset for asset in client.list_release_assets(release_id)}
...
plan = _plan_entry_assets(index, entry, encrypt)
if all(part["asset_name"] in existing_assets for part in plan):
    parts_meta = [{"order": part["chunk_index"],
                   "asset_name": part["asset_name"],
                   "asset_id": existing_assets[part["asset_name"]]["id"],
                   "size": int(existing_assets[part["asset_name"]].get("size") or 0)} for part in plan]
    ... ArchiveItem(..., source_sha256="", content_type=existing_assets[first]["content_type"], parts=parts_meta)
    emit_progress(progress, "item_skipped", {...})
    completed_items += 1
    continue
```

- **Whole-entry skip:** if *every* planned chunk asset name already exists, the file is not read, not
  hashed, not uploaded. `source_sha256` is set to `""` and `content_type` is taken from the **existing
  asset's** reported content type.
- **Per-chunk skip (partial resume):** inside `_upload_entry`, for each planned chunk:

```python
if asset_name in existing_assets:
    asset = existing_assets[asset_name]
    parts_meta.append({"order": chunk_index, "asset_name": asset_name,
                       "asset_id": asset["id"], "size": int(asset.get("size") or 0)})
    src.seek(part["chunk_offset"] + part["chunk_length"])
    continue
```
  The source handle is fast-forwarded past the already-uploaded chunk. Note that in this path the entry's
  `source_sha256` **is** computed (`sha = _sha256_file(source_path)` runs before the loop), so partially
  resumed entries keep a real hash while fully skipped entries do not.
- Bundle mode: `_upload_bundle_archive` **rebuilds the zip locally first**, then applies the same
  all-parts-exist check against the bundle's planned asset names.
- `emit_progress(..., "archive_resumed", {"completed_items": N, "total_items": M})` fires when any entry
  was skipped.

### 9.3 Idempotency at the API layer

`422 Unprocessable Entity` with an `errors[].code == "already_exists"` is treated as success:

```python
def _github_error_is_already_exists(exc, field=None) -> bool:
    if not isinstance(exc, GitHubError) or exc.status != 422: return False
    payload = json.loads(exc.response_body or "")
    for item in payload.get("errors") or []:
        if str(item.get("code")).strip() != "already_exists": continue
        if field and str(item.get("field")).strip() != field: continue
        return True
    return False
```

- Release creation races on `field="tag_name"` → re-fetch by tag; accepted only if its body decodes as an archive.
- Asset uploads race on `field="name"` → `_find_release_asset_by_name` (re-lists assets) and use that asset.

Application-level retry wrapper (`_retry`): `attempts = max(1, retries)` (default `retries=3`), sleep
`1.0s` then doubling, capped at `8.0s`; final failure raises
`RuntimeError("{operation} failed after {n} attempt(s): {err}")`.

### 9.4 Manifest at the end of a resumed run

`_manifest.json` is always rewritten last: if it already exists it is **deleted** (`delete stale manifest`,
retried) and then re-uploaded. The same delete-then-upload dance happens in `append_to_archive` and
`delete_archive_file`, which additionally **delete `_cover.jpg`** afterwards so it is regenerated lazily.

### 9.5 Download-side resume

`download_archive(..., skip_existing=True)` (the default): an item is skipped when its destination file
already exists (`target.exists()`), emitting `item_skipped`. **No size or hash verification.** In bundle
mode the whole bundle download is skipped only if **all** members already exist; otherwise the bundle is
fetched once and individual existing members are skipped during extraction.

### 9.6 Concurrency notes for a port

Uploads use `ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending) or 1)))`, default
`workers=2`. `existing_assets` is a snapshot taken **once**, before the pool starts, so two workers
racing on the same asset name rely on the 422 `already_exists` handling rather than the snapshot.

---

## 10. GitHub REST calls actually used

Base URLs (`api.py`): `GITHUB_API_BASE = "https://api.github.com"`,
`GITHUB_UPLOADS_BASE = "https://uploads.github.com"`.

### 10.1 Default headers (on every request)

```python
{
  "Authorization": f"Bearer {token}",
  "Accept": "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",     # DEFAULT_API_VERSION
  "User-Agent": "github-drive",
}
```

Default request timeout 60 s; upload/download calls use `max(timeout, 600)`.

### 10.2 The calls

| # | Purpose | Method + path | Headers / params / body |
|---|---|---|---|
| 1 | Repo info | `GET https://api.github.com/repos/{owner}/{repo}` | defaults. 404 is the "create it" signal. |
| 2 | **Create repo** | `POST https://api.github.com/user/repos` | defaults + JSON body `{"name": repo, "private": bool(private), "description": description, "auto_init": true}`. `description` defaults to `"GitHub Drive archives"`. Called by `ensure_repo` only after (1) returns 404. |
| 3 | Viewer login | `GET https://api.github.com/user` | defaults; reads `.login`. |
| 4 | **List releases (all)** | `GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=100` | defaults. Follow `Link: <url>; rel="next"` until absent; subsequent requests send the next URL with **no params**. |
| 5 | **List releases (page)** | `GET .../releases?per_page={per_page}&page={page}` | `page = max(1, page)`, `per_page = max(1, min(per_page, 100))`. `has_more` = a `rel="next"` link exists. |
| 6 | **Get release by tag** | `GET https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}` | defaults. **404 → returns `None`** (not an error); any other 4xx/5xx raises. Tag is interpolated raw (not URL-escaped) — safe because tags are `github-drive-` + hex. |
| 7 | Get release by id | `GET .../releases/{release_id}` | defaults. |
| 8 | **Create release** | `POST https://api.github.com/repos/{owner}/{repo}/releases` | defaults + JSON body `{"tag_name": tag, "name": name, "body": body, "draft": false, "prerelease": bool(private_release)}` (`"target_commitish"` added only if supplied — the archive code never supplies it). |
| 9 | Update release | `PATCH .../releases/{release_id}` | defaults + JSON body of only the changed fields (`name`, `body` in practice). |
| 10 | **Delete release** | `DELETE .../releases/{release_id}` | defaults. 204 expected. |
| 11 | **Delete git ref (tag)** | `DELETE https://api.github.com/repos/{owner}/{repo}/git/refs/tags/{tag}` | defaults. **404 is swallowed**; other errors re-raise. Called after (10) when `delete_tag=True` (the default in `delete_archive`). |
| 12 | List release assets | `GET .../releases/{release_id}/assets?per_page=100` | defaults; `Link`-paginated the same way as (4). |
| 13 | **Upload asset (file)** | `POST https://uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets?name={asset_name}[&label={label}]` | defaults **plus** `Content-Type: {content_type}` (overrides nothing else) and `Content-Length: {os.path.getsize(file_path)}`. Body = raw file bytes (streamed from an open handle). `label` param only when a label is passed — the archive code never passes one. Timeout `max(timeout, 600)`. |
| 14 | Upload asset (stream) | same URL, `?name={asset_name}` only | same headers, `Content-Length` = caller-supplied; body streamed. Used by the browser single-file fast path; the stream is `seek()`ed back to its start position before each retry. |
| 15 | Upload asset (bytes) | same URL, `?name={asset_name}` only | same headers, `Content-Length = len(payload)`. Used for `_manifest.json` (`application/json`) and `_cover.jpg` (`image/jpeg`). |
| 16 | Delete asset | `DELETE https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}` | defaults. |
| 17 | **Download asset (to disk)** | `GET https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}` | defaults **with `Accept` replaced by `application/octet-stream`**; `stream=True`, `allow_redirects=True`, timeout `max(timeout, 600)`; written in 1 MiB chunks (`chunk_size = 1024*1024`). Note this is the **api.github.com** asset endpoint, not `browser_download_url`. |
| 18 | Download asset (bytes) | same as (17) without streaming | `allow_redirects=True`; optional in-process cache keyed by asset id, TTL 600 s, only for payloads ≤ 2 MiB. Used for `_manifest.json` and `_cover.jpg`. |

Note: the upload endpoints inherit `Accept: application/vnd.github+json` from the defaults *plus* the
overriding `Content-Type`; the download endpoints override `Accept` to `application/octet-stream`.
The redirect to the S3-style storage host must be followed (`allow_redirects=True`); Android's
`OkHttpClient` follows redirects by default but **strips the `Authorization` header only on
cross-host redirects** — verify this against GitHub's signed redirect URL, which does not need the header.

### 10.3 Retry / rate-limit policy (`_request_with_retries`)

- `max_attempts = 3`.
- Retryable: status in `{429, 500, 502, 503, 504}`; or `403` whose body contains `"secondary rate limit"`
  or `"rate limit"`, or which carries a `Retry-After` header, or `X-RateLimit-Remaining: 0`.
- Delay: honour `Retry-After` clamped to `[1.0, 30.0]`; else if `X-RateLimit-Remaining == 0` use
  `X-RateLimit-Reset - now` clamped to ≤ 30 s; else if "secondary rate limit" in body,
  `max(5.0, min(2**(attempt-1), 60.0)) + rand(0,2)` capped at 120 s; else
  `min(2**(attempt-1), 30.0) + rand(0,1)` capped at 60 s.
- Non-retryable or exhausted → `GitHubError(status, reason, body)`; transport exhaustion →
  `GitHubError(0, "{op} failed after 3 attempts", str(err))`.

### 10.4 Client-side caching (affects observed freshness, not the wire format)

In-process, per `(sha256(token)[:16] + ":" + owner/repo)` namespace: releases list (30 s), release by
id/tag (30 s), assets list (30 s), asset bytes ≤ 2 MiB (600 s). Every mutating call
(`create_release`, `update_release`, `delete_release`, `delete_tag`, `upload_asset*`, `delete_asset`)
invalidates the relevant buckets.

---

## 11. Kotlin porting checklist (byte-identity critical path)

1. **Tag**: `"github-drive-" + archiveId.lowercase()`; `archiveId` = 12 uppercase hex chars from a UUID4.
2. **Title**: `"GitHub Drive | ${name.replace(Regex("\\s+"), " ").trim().ifEmpty { "archive" }} | $n items"`.
3. **Body**: fixed preamble line, blank line, fenced block, `GITHUB_DRIVE_ARCHIVE=` + **compact**
   (`,`/`:`, no spaces) **ASCII-escaped** JSON, trailing newline.
4. **Timestamps**: `yyyy-MM-dd'T'HH:mm:ss+00:00` — never `Z`.
5. **Manifest**: Python `json.dumps(..., indent=2)` layout, UTF-8, `\uXXXX` for non-ASCII, **no trailing
   newline**; preserve the exact key order for the producer path you are emulating (§2.1 vs §2.7), and the
   `parts` key-order difference in §2.3.
6. **Entry order**: lexicographic on the full relative path, files only, `/` separators.
7. **Asset names**: `%04d-` prefix; `/`→`__`; `[^A-Za-z0-9._-]+` → `-`; strip leading/trailing `-.`;
   truncate to 180 (plain) / 160 (chunked, then append `.part%04d`); `.enc` last.
8. **Chunking**: threshold `1_900_000_000` (minus 33 when encrypting), `<=` is single-asset,
   chunks sized exactly `threshold` except the last.
9. **Encryption**: `GDRV` + `0x01` + 12-byte nonce + **16-byte tag** + ciphertext, AES-GCM, no AAD,
   128-bit tag — remember Java puts the tag after the ciphertext, so splice on both encrypt and decrypt.
10. **Key**: no passphrase KDF; for the hosted web app it is
    `HMAC-SHA256(baseKey, "archive:" + username.trim().lowercase())[0..15]`.
11. **Bundling**: `≥1000` files, or `≥256` files with mean size `≤ 8 MiB`; ZIP/DEFLATE(6)/Zip64,
    `arcname = relative_path`; do not chase byte-equality of the zip container itself.
12. **Resume**: skip on asset-name presence alone; on whole-entry skip write `source_sha256 = ""`.
13. **Always rewrite `_manifest.json` last** (delete then upload) and delete `_cover.jpg` after
    append/delete so it regenerates.

---

## 12. Consolidated list of ambiguities / non-determinism

1. **No archive passphrase KDF exists** — no PBKDF2/scrypt, no salt, no iteration count, no salt field in
   the GDRV header. (§6.2)
2. **`limits.py` holds no size limits** — it is a rate-limiter; the real limits live in `webapp.py`. (§7)
3. **Two manifest shapes** differing in key order and in the presence of `source_type`, plus
   `total_items` meaning `len(entries)` vs `len(items)`. (§2.7)
4. **`parts[]` key order differs** between fresh uploads and rewritten manifests. (§2.3)
5. **ZIP bundles are not byte-reproducible** across implementations (mtime, permissions, extra fields,
   deflate implementation). (§4.2)
6. **`_cover.jpg` bytes are not specified** — three producers, three different encoders/qualities. (§8.3)
7. **`cover_asset_name` can claim a cover that never materializes** (video-only archives). (§8.2)
8. **The no-manifest fallback cannot handle chunked files** and would misread `_cover.jpg` as an entry. (§3.5)
9. **`{order:04d}` is a minimum width** — archives with > 10 000 entries produce 5-digit prefixes, and the
   fallback decoder's `head.isdigit()` still handles them, but sort-by-name breaks.
10. **Sanitized asset names are lossy and can collide** across different source paths; uniqueness rests
    solely on the `order` prefix, so `order` must be unique within a release.
11. `collect_file_entries` uses `str(Path.relative_to(...))`, which yields `\` separators on Windows; the
    code does not normalize there. Android should always emit `/`.
12. **Whole-file in-memory encryption** (`src.read()`) — the format does not require it, but chunk sizes
    were chosen assuming it.
