# github-drive

Upload and restore arbitrary files of any type as **GitHub Release** archives.

Each upload creates a new GitHub Release in a designated repository, and each file becomes a release asset (raw bytes, up to 2 GB per asset, unlimited count).

## Features

- Upload a single file
- Upload a folder of files (any types, recursive by default)
- Each upload creates a versioned GitHub Release archive in a target repo
- Original folder structure is preserved through a per-archive `_manifest.json`
- Parallel uploads and downloads
- Auto-bundle mode for tiny-file-heavy uploads to cut request count dramatically
- Optional client-side AES-128-GCM encryption before upload
- Localhost web frontend or CLI
- Resume: continue an interrupted upload into an existing release by tag, archive id, or release id
- Delete archives by tag, archive id, or release id

## Installation

```sh
git clone <repo-url> github-drive
cd github-drive
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## GitHub Setup

You need:

1. A **Personal Access Token (PAT)** with the `repo` scope:
   - Classic PAT: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → `repo` scope.
   - Fine-grained PAT: select the target repository, with `Contents: Read and write` and `Metadata: Read-only`.
2. A **target repository** to hold archives. It can be empty; the tool will create a release per archive. You can let the CLI create it.

Authenticate:

```sh
python -m github_drive auth --token ghp_xxx --repo your-username/github-drive-archives --create-repo
```

Inspect auth state:

```sh
python -m github_drive auth-status
```

The token is saved to `~/.github-drive/token.json` (chmod 600). You can also configure via env vars:

- `GITHUB_DRIVE_TOKEN` (or `GITHUB_TOKEN`)
- `GITHUB_DRIVE_REPO` (e.g. `owner/repo`)

## CLI Usage

Show commands:

```sh
python -m github_drive -h
```

Upload a file or folder:

```sh
python -m github_drive upload /path/to/folder --workers 4 --retries 3
```

Upload layout is chosen automatically. Tiny-file-heavy folders are bundled before upload; larger files are chunked when needed.

Resume an interrupted upload into an existing archive:

```sh
python -m github_drive upload /path/to/folder --resume-tag github-drive-XXXXXX
python -m github_drive upload /path/to/folder --resume-archive-id XXXXXX
python -m github_drive upload /path/to/folder --resume-release-id 12345678
```

Encrypt before upload (use the same key on download):

```sh
python -m github_drive upload /path/to/folder --encrypt --key "my-passphrase"
python -m github_drive download --tag github-drive-XXXXXX /path/to/output --decrypt --key "my-passphrase"
```

List archives in the configured repo:

```sh
python -m github_drive list
```

Download an archive (by tag, archive id, or release id):

```sh
python -m github_drive download --tag github-drive-XXXXXX /path/to/output --workers 4
python -m github_drive download --archive-id XXXXXX /path/to/output
python -m github_drive download --release-id 12345678 /path/to/output
```

Delete an archive (release + git tag):

```sh
python -m github_drive delete --tag github-drive-XXXXXX
```

## Localhost Frontend

```sh
python -m github_drive web
```

Starts a local server at `http://127.0.0.1:8765`. The frontend is **multi-tenant**: every visitor signs in with their own username and password and supplies their own GitHub PAT and repository. Archives, tasks, and credentials are isolated per user.

### Multi-user model

| Concern | Behaviour |
|---|---|
| Account store | `~/.github-drive/users.json`. One JSON record per user, scrypt-hashed password, atomically written. |
| Session | Flask signed cookie, HttpOnly + SameSite=Lax. Lifetime: 14 days. Signed with `GITHUB_DRIVE_SESSION_SECRET`. |
| GitHub PAT | Encrypted at rest with AES-128-GCM, key derived from `GITHUB_DRIVE_SESSION_SECRET` + username. Rotating the session secret invalidates stored PATs and forces every user to re-enter theirs. |
| Encryption key for archive contents | Derived from `GITHUB_DRIVE_ENCRYPTION_KEY` (or session secret fallback) HMAC-mixed with the username, so two users on the same server cannot decrypt each other's archives. |
| Tasks | Each task records its `user_id`; `/api/tasks` only returns the caller's. Background runners use the per-user PAT. |
| Signup | **Disabled by default.** Admin creates accounts via CLI. Set `GITHUB_DRIVE_ALLOW_SIGNUP=1` to expose `/signup`. |

### Provisioning users (CLI, on the host)

```sh
python -m github_drive users add alice         # prompts for password
python -m github_drive users list
python -m github_drive users set-password alice
python -m github_drive users remove alice
```

The user then signs in at the web URL, configures their own PAT and target repo, and starts uploading. Their archives live under their own GitHub account; the server only stores the encrypted PAT.

### CLI vs web

The CLI (`github-drive upload`, `download`, etc.) is **single-tenant**. It uses `~/.github-drive/token.json` as before — meant for the operator running it locally. The multi-user experience is web-only.

## Hosting

The web app is deployable as a single-process Flask/Gunicorn service. The repository ships with three deployment hooks, all driving the same WSGI entry point `github_drive.webapp:create_app()`:

| File | Purpose |
|---|---|
| [render.yaml](render.yaml) | Render blueprint: build, start, health check, env-var slots |
| [Procfile](Procfile) | Heroku/Railway/Fly Procfile-style platforms |
| [Dockerfile](Dockerfile) | Portable container for any host (Fly.io, Cloud Run, ECS, self-hosted) |

### Required environment variables on a hosted instance

| Variable | Required? | Notes |
|---|---|---|
| `GITHUB_DRIVE_SESSION_SECRET` | yes (auto-generated otherwise) | Random 64-hex string used to sign Flask sessions. Render auto-generates it. May rotate freely once you have set `GITHUB_DRIVE_ENCRYPTION_KEY`. |
| `GITHUB_DRIVE_ENCRYPTION_KEY` | required if you encrypt | Stable AES key (hex or base64, 16/24/32 bytes). Generate with `python -m github_drive gen-key`. Set once and pin — rotating it makes prior encrypted archives unreadable. |
| `GITHUB_DRIVE_TOKEN` | recommended | GitHub PAT with `repo` scope. If set, the UI does not need to ask for it. |
| `GITHUB_DRIVE_REPO` | recommended | Target repository as `owner/repo`. |
| `GITHUB_DRIVE_BASIC_AUTH` | optional outer gate | `user:password`. When set, every route (including `/login`) is wrapped in HTTP Basic. Useful as an outer perimeter on top of per-user logins; not a replacement for them. `/healthz` stays open for platform probes. |
| `GITHUB_DRIVE_ALLOW_SIGNUP` | optional | `1` to expose `/signup`. Default off — admin provisions accounts with `github-drive users add`. |
| `GITHUB_DRIVE_USER_ID` | optional, legacy | Namespace for the legacy derivation. Only consulted when `GITHUB_DRIVE_ENCRYPTION_KEY` is not set. |
| `GITHUB_DRIVE_ENCRYPT` | optional | `1` to encrypt every web upload by default. |
| `GITHUB_DRIVE_MAX_UPLOAD_BYTES` | optional | Max single-request upload size. Default 5 GB. |
| `PORT` | injected by host | Standard PaaS variable; the app reads it automatically. |

### Hosted vs local: ephemeral filesystem

On Render, Fly.io, etc. the filesystem is wiped on every restart. If you configure the PAT through the web UI it will be lost on the next restart — set `GITHUB_DRIVE_TOKEN` and `GITHUB_DRIVE_REPO` as platform secrets so the app self-restores on boot.

### Deploy targets

**Render:** push the repo and click "New Blueprint" — Render reads `render.yaml`, generates `GITHUB_DRIVE_SESSION_SECRET`, and prompts you for the rest. Health check at `/healthz`.

**Docker (any host):**

```sh
docker build -t github-drive .
docker run --rm -p 8765:8765 \
  -e GITHUB_DRIVE_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')" \
  -e GITHUB_DRIVE_TOKEN=ghp_xxx \
  -e GITHUB_DRIVE_REPO=your-username/github-drive-archives \
  -e GITHUB_DRIVE_BASIC_AUTH=admin:supersecret \
  github-drive
```

**Heroku/Railway/Fly:** the `Procfile` is the entry point. Push the repo, then set the environment variables above through the platform dashboard.

### Hosting limits

- Single Gunicorn worker, 8 threads. The in-memory task list is shared across threads but not across workers, so do not raise `--workers` above 1 without adding a real task store.
- Per-asset cap: 2 GB (GitHub Releases). Larger files need to be split before upload.
- API rate limit: 5,000 authenticated requests/hour per token. Auto-bundle mode exists mainly to protect this budget on tiny-file-heavy uploads.

## Encryption

- AES-128/192/256-GCM is applied client-side per file before upload when `--encrypt` is set (CLI) or `GITHUB_DRIVE_ENCRYPT=1` (web).
- Encrypted assets land on the release with a `.enc` suffix.
- The encryption key is resolved in this order:
  1. `--key <passphrase>` on the CLI (utf-8 padded/truncated to 16 bytes; for one-off testing).
  2. **`GITHUB_DRIVE_ENCRYPTION_KEY`** — hex- or base64-encoded raw key (recommended for hosted use). Set this once and keep it stable across redeploys; rotating it makes prior archives unreadable.
  3. Legacy fallback: HMAC-SHA256(`GITHUB_DRIVE_SESSION_SECRET`, `GITHUB_DRIVE_USER_ID`) truncated to 16 bytes. This is only used when no encryption key is set and was the original behaviour. Migrate off this path if you redeploy frequently.

### Generate a stable key

```sh
python -m github_drive gen-key                # 32-byte AES-256 key (default), prints hex
python -m github_drive gen-key --bytes 16     # 16-byte AES-128 key
```

Set the printed value as `GITHUB_DRIVE_ENCRYPTION_KEY` in your hosting platform's secret manager. Treat it like a password — anyone holding it can decrypt your archives.

### Migrating from the legacy derivation

If you have archives encrypted under the old derivation and you do not want to re-upload them, export the legacy key once and pin it:

```sh
GITHUB_DRIVE_SESSION_SECRET=your-current-session-secret \
GITHUB_DRIVE_USER_ID=default \
python -m github_drive gen-key --from-legacy
```

Save the hex output as `GITHUB_DRIVE_ENCRYPTION_KEY`. After that, the session secret can rotate freely without breaking decryption of older archives.

## Large files

Files above the per-asset chunk threshold are split into multiple release assets at upload time and rejoined on download. This is automatic — no flag to set:

- Default chunk size: **1.9 GB**, leaving headroom under GitHub's 2 GB asset cap. Tunable via `GITHUB_DRIVE_CHUNK_BYTES` (raw bytes, e.g. `1500000000` for 1.5 GB).
- Chunk asset names: `NNNN-<file>.partKKKK[.enc]`. The original file name is reconstructed from the manifest, so the path inside the destination folder is preserved.
- Encryption + chunking: each chunk is encrypted independently with its own AES-GCM nonce + tag. RAM use during encrypt/decrypt is bounded by chunk size, not total file size — so a 50 GB encrypted upload only needs ~1.9 GB of RAM at a time.
- Resume: if some chunks already exist on the release (e.g. a previous run failed mid-way), they are skipped and only the missing chunks are uploaded.
- Download disk usage: peak is roughly one chunk; each downloaded chunk is decrypted (if needed), appended to the target file, and removed before the next one is fetched.

Single-asset archives produced before chunking landed continue to download exactly as before — the manifest schema is backwards compatible.

## Limits

- Per-asset hard limit on the wire: 2 GB. **Single source files are no longer bounded by this** — they are split transparently. The ceiling now is whatever your scratch disk can hold during upload (GitHub itself imposes no documented per-release total).
- Repo-wide release storage soft limit: depends on plan; check GitHub's quotas.
- API rate limit: 5,000 authenticated requests/hour. Splitting a file into N chunks costs N API calls, so very large files burn through the budget faster.

## Tiny-file-heavy folders

Uploading 10,000 small files as 10,000 separate release assets is slow and burns rate limit quickly. The tool now detects tiny-file-heavy trees automatically, first packs them into a ZIP bundle, then uploads that bundle as one logical archive asset (chunked if needed). Download restores the original file layout from the manifest-backed bundle automatically.

## Environment Variables

| Variable | Purpose |
|---|---|
| `GITHUB_DRIVE_TOKEN` | GitHub PAT with `repo` scope |
| `GITHUB_DRIVE_REPO` | Target archives repo as `owner/repo` |
| `GITHUB_DRIVE_SESSION_SECRET` | Required by the web app for Flask sessions. Auto-generated if missing (warns). Also used as the legacy fallback for encryption key derivation when `GITHUB_DRIVE_ENCRYPTION_KEY` is not set. |
| `GITHUB_DRIVE_ENCRYPTION_KEY` | Stable AES key (hex or base64, 16/24/32 bytes). Survives redeploys, unlike the session secret. |
| `GITHUB_DRIVE_USER_ID` | Optional namespace for the derived encryption key (default `default`) |
| `GITHUB_DRIVE_ENCRYPT` | Set to `1`/`true` to encrypt all uploads from the web flow |
| `GITHUB_DRIVE_ENCRYPTION_KEY` | Optional CLI fallback if `--key` is not passed |
| `GITHUB_DRIVE_BASIC_AUTH` | `user:password` to gate the web app behind HTTP Basic |
| `GITHUB_DRIVE_MAX_UPLOAD_BYTES` | Max bytes per upload request (default 5 GB) |
| `GITHUB_DRIVE_CHUNK_BYTES` | Per-chunk upload size when splitting large files (default ~1.9 GB) |
