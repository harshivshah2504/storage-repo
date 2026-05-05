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

The CLI token is saved to `GITHUB_DRIVE_STATE_DIR/token.json` when that env var is set, otherwise `~/.github-drive/token.json` (chmod 600). The web app does not mirror env tokens to disk unless you explicitly set `GITHUB_DRIVE_MIRROR_ENV_TOKEN=1`. You can also configure CLI auth via env vars:

- `GITHUB_DRIVE_TOKEN` (or `GITHUB_TOKEN`)
- `GITHUB_DRIVE_REPO` (e.g. `owner/repo`)

## CLI Usage

Show commands:

```sh
python -m github_drive -h
```

Upload a file or folder:

```sh
python -m github_drive upload /path/to/folder --workers 2 --retries 3
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
python -m github_drive download --tag github-drive-XXXXXX /path/to/output --workers 2
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

Starts a local server at `http://127.0.0.1:8765`. The frontend is **multi-tenant**: every visitor signs in with either a local account or GitHub OAuth and supplies their own GitHub PAT and repository. Archives, tasks, and credentials are isolated per user.

### Multi-user model

| Concern | Behaviour |
|---|---|
| Account store | **Postgres** when `GITHUB_DRIVE_DATABASE_URL` (or `DATABASE_URL`) is set — strongly recommended for any hosted deployment. Schema is created on first connect. JSON file (`GITHUB_DRIVE_STATE_DIR/users.json`, otherwise `~/.github-drive/users.json`) is the local-dev fallback. Records are identical between backends; switch with `github-drive users migrate-to-db`. |
| Session | Flask signed cookie, HttpOnly + SameSite=Lax. Lifetime: 14 days. Signed with `GITHUB_DRIVE_SESSION_SECRET`. State-changing routes also require a CSRF token. |
| GitHub PAT | Encrypted at rest with AES-128-GCM, key derived from `GITHUB_DRIVE_SESSION_SECRET` + username. Rotating the session secret invalidates stored PATs and forces every user to re-enter theirs. |
| Encryption key for archive contents | Derived from `GITHUB_DRIVE_ENCRYPTION_KEY` (or session secret fallback) HMAC-mixed with the username, so two users on the same server cannot decrypt each other's archives. |
| Tasks | Each task records its `user_id`; `/api/tasks` only returns the caller's. Background runners use the per-user PAT. |
| Signup | Controlled by `GITHUB_DRIVE_ALLOW_SIGNUP` (`true` by default for local/dev compatibility). To allow only GitHub-based self-serve signup, set `GITHUB_DRIVE_ALLOW_SIGNUP=false` and `GITHUB_DRIVE_ALLOW_GITHUB_OAUTH_SIGNUP=true`. |

### External database (Postgres)

For any hosted deployment, run on Postgres rather than the JSON file. The JSON path is fine for local development but is fragile on ephemeral filesystems and under concurrent writes. Postgres gives you durable storage, atomic writes, and survives instance restarts.

| Provider | How to wire up |
|---|---|
| **Render** | The bundled `render.yaml` provisions a managed Postgres database (`github-drive-db`, free tier) and injects the connection string as `GITHUB_DRIVE_DATABASE_URL`. Just deploy the blueprint. |
| **Supabase** | Project settings → Database → Connection string → URI. Use the **direct** (non-pooled) one. Set it as `GITHUB_DRIVE_DATABASE_URL`. |
| **Neon / Railway / Fly Postgres / RDS / etc.** | Any Postgres ≥ 13. Copy the connection string to `GITHUB_DRIVE_DATABASE_URL`. SSL is honored if the URL contains `?sslmode=require`. |

Schema is created automatically on the first connection. Two tables are used:

```
users(username PK, salt, password_hash, password_kdf, created_at, updated_at)
github_credentials(username PK→users, token_encrypted, owner, repo, updated_at)
```

Operational helpers:

```sh
python -m github_drive users backend          # show active backend + DB connectivity
python -m github_drive users migrate-to-db    # copy users.json into Postgres (one-time)
```

`migrate-to-db` is idempotent and never overwrites users that already exist in the database.

### Provisioning users (CLI, on the host)

```sh
python -m github_drive users add alice         # prompts for password
python -m github_drive users list
python -m github_drive users set-password alice
python -m github_drive users remove alice
```

The user then signs in at the web URL, configures their own PAT and target repo, and starts uploading. Their archives live under their own GitHub account; the server only stores the encrypted PAT.

### CLI vs web

The CLI (`github-drive upload`, `download`, etc.) is **single-tenant**. It uses `GITHUB_DRIVE_STATE_DIR/token.json` when set, otherwise `~/.github-drive/token.json` — meant for the operator running it locally. The multi-user experience is web-only.

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
| `GITHUB_DRIVE_TOKEN` | optional, CLI/operator use | GitHub PAT with `repo` scope. The multi-user web UI stores each user's PAT separately after login. |
| `GITHUB_DRIVE_REPO` | optional, CLI/operator use | Target repository as `owner/repo`. |
| `GITHUB_DRIVE_BASIC_AUTH` | optional outer gate | `user:password`. When set, every route (including `/login`) is wrapped in HTTP Basic. Useful as an outer perimeter on top of per-user logins; not a replacement for them. `/healthz` stays open for platform probes. |
| `GITHUB_DRIVE_ADMIN_USERS` | optional | Comma- or space-separated usernames that may list and remove accounts through admin APIs. |
| `GITHUB_DRIVE_ALLOW_SIGNUP` | optional | `true`/`false`; defaults to `true`. Set to `false` on public hosted instances after provisioning users. The bundled Render blueprint sets this to `false`. |
| `GITHUB_DRIVE_ALLOW_GITHUB_OAUTH_SIGNUP` | optional | `true`/`false`; defaults to the value of `GITHUB_DRIVE_ALLOW_SIGNUP`. Set this to `true` with password signup disabled to make GitHub OAuth the only public signup path. |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | optional | Enables "Continue with GitHub" login/signup. Create a GitHub OAuth App and set the callback URL to `/auth/github/callback`. |
| `GITHUB_OAUTH_REDIRECT_URI` | optional | Explicit callback URL, useful behind custom domains. Example: `https://your-domain.example/auth/github/callback`. |
| `GITHUB_OAUTH_SCOPE` | optional | Defaults to `repo read:user user:email`, so the OAuth token can access the user's chosen archive repo. |
| `GITHUB_DRIVE_TURNSTILE_SITE_KEY` / `GITHUB_DRIVE_TURNSTILE_SECRET_KEY` | optional but recommended for public deployments | Enables Cloudflare Turnstile on login, password signup, and GitHub OAuth start. If set, the app validates `cf-turnstile-response` server-side before any auth attempt proceeds. |
| `GITHUB_DRIVE_MIRROR_ENV_TOKEN` | optional, legacy | `1` to copy `GITHUB_DRIVE_TOKEN` into `token.json` at web startup. Leave unset for hosted multi-user deployments to avoid storing a plaintext operator PAT on disk. |
| `GITHUB_DRIVE_ENABLE_DB_CHECK` | optional diagnostic | `1` to enable `/api/db-check` for signed-in users. It is disabled by default so public deployments do not expose database details. |
| `GITHUB_DRIVE_DB_WARM_TOKEN` | optional warm-up secret | Enables `/warm-db`, a secret-protected endpoint for external cron jobs that should wake Postgres/Neon by running `SELECT 1`. Pass the token as `?token=...` or `X-Warm-Token`. |
| `GITHUB_DRIVE_USER_ID` | optional, legacy | Namespace for the legacy derivation. Only consulted when `GITHUB_DRIVE_ENCRYPTION_KEY` is not set. |
| `GITHUB_DRIVE_ENCRYPT` | optional | `1` to encrypt every web upload by default. |
| `GITHUB_DRIVE_MAX_UPLOAD_BYTES` | optional | Max single-request upload size. Default 5 GB. |
| `GITHUB_DRIVE_USER_MAX_UPLOAD_BYTES` | optional | Per browser upload cap after files are staged. Default 2 GB. |
| `GITHUB_DRIVE_MAX_FILES_PER_UPLOAD` | optional | Max files accepted in one browser upload. Default 5000. |
| `GITHUB_DRIVE_AUTH_RATE_LIMIT` / `GITHUB_DRIVE_AUTH_RATE_WINDOW_SECONDS` | optional | Login/signup/OAuth attempt limiter. Defaults: 20 attempts per 15 minutes per IP. |
| `GITHUB_DRIVE_USER_ACTION_RATE_LIMIT` / `GITHUB_DRIVE_USER_ACTION_RATE_WINDOW_SECONDS` | optional | Per-user API action limiter. Defaults: 60 actions per 60 seconds. |
| `GITHUB_DRIVE_MAX_ACTIVE_TASKS_GLOBAL` | optional | Global active transfer cap. Default `0` (disabled). Set to `1` on free/shared hosting to turn queued transfers into a simple in-process FIFO. |
| `GITHUB_DRIVE_MAX_ACTIVE_TASKS_PER_USER` | optional | Max queued/running transfers per user. Default 3. Use `0` to disable. |
| `GITHUB_DRIVE_RELEASES_CACHE_TTL_SECONDS` / `GITHUB_DRIVE_RELEASE_CACHE_TTL_SECONDS` / `GITHUB_DRIVE_RELEASE_ASSETS_CACHE_TTL_SECONDS` | optional | In-memory GitHub metadata cache TTLs. Defaults: 30 seconds each. These sharply reduce repeated release/asset listing calls on the home page and archive browser. |
| `GITHUB_DRIVE_ASSET_BYTES_CACHE_TTL_SECONDS` / `GITHUB_DRIVE_ASSET_BYTES_CACHE_MAX_BYTES` | optional | Cache small asset payloads such as `_manifest.json` and generated `_cover.jpg`. Defaults: 600 seconds and 2 MiB. |
| `GITHUB_DRIVE_ARCHIVES_PAGE_SIZE` | optional | Number of archives the home page loads per request. Default `24`. Older archives are fetched with the UI's "Load more" control instead of walking the full releases history on first load. |
| `GITHUB_DRIVE_STATE_DIR` | optional, only relevant without a database | Directory for persistent local state (`users.json`, `token.json`). Use a mounted disk if you have one. Ignored once `GITHUB_DRIVE_DATABASE_URL` is set, which is the recommended path on platforms without persistent disks (e.g. Render free tier). |
| `GITHUB_DRIVE_DATABASE_URL` | strongly recommended on hosted installs | Postgres connection string. Accepts both `postgres://` and `postgresql://` schemes. When set, all account data lives in the database instead of `users.json`. Falls back to `DATABASE_URL` if the prefixed version is unset. |
| `GITHUB_DRIVE_DB_MIN_CONNECTIONS` / `GITHUB_DRIVE_DB_MAX_CONNECTIONS` | optional | Pool sizing. Defaults: 0 / 4. |
| `PORT` | injected by host | Standard PaaS variable; the app reads it automatically. |

### Hosted vs local: ephemeral filesystem

On Render, Fly.io, etc. the filesystem is wiped on every restart unless you mount persistent storage. That affects both the operator token file and the multi-user account store, so hosted signups can disappear after a restart if `users.json` lives on the ephemeral root filesystem.

For Render, mount a persistent disk and point `GITHUB_DRIVE_STATE_DIR` at it. The included [render.yaml](render.yaml) does this by mounting `/var/data` and storing app state in `/var/data/github-drive`. Without that, a newly created hosted user may be able to sign up once and then fail to log back in after the service restarts because their record is gone.

`/healthz` is intentionally minimal and only returns `{"ok": true}` for platform liveness checks. For database diagnostics, temporarily set `GITHUB_DRIVE_ENABLE_DB_CHECK=1`, sign in, and open `/api/db-check`; disable it again after troubleshooting.

If you are using Neon free and want to keep the database warm, set `GITHUB_DRIVE_DB_WARM_TOKEN` and point an external cron at:

```text
https://your-app.onrender.com/warm-db?token=YOUR_SECRET
```

Run it every 4 minutes so Neon does not scale to zero between requests.

### Deploy targets

**Render:** push the repo and click "New Blueprint" — Render reads `render.yaml`, provisions Postgres, generates `GITHUB_DRIVE_SESSION_SECRET`, disables public signup by default, and prompts you for the rest. Health check at `/healthz`.

### Free public beta checklist

If you want the safest low-cost public setup for this repo without redesigning the transfer architecture yet:

1. Set `GITHUB_DRIVE_ALLOW_SIGNUP=false`.
2. Set `GITHUB_DRIVE_ALLOW_GITHUB_OAUTH_SIGNUP=true`.
3. Configure `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, and `GITHUB_OAUTH_REDIRECT_URI`.
4. Configure `GITHUB_DRIVE_TURNSTILE_SITE_KEY` and `GITHUB_DRIVE_TURNSTILE_SECRET_KEY`.
5. Use Neon pooled Postgres and set `GITHUB_DRIVE_DB_MAX_CONNECTIONS=4`.
6. Set `GITHUB_DRIVE_MAX_ACTIVE_TASKS_GLOBAL=1`.
7. Set `GITHUB_DRIVE_MAX_ACTIVE_TASKS_PER_USER=1`.
8. Keep `GITHUB_DRIVE_DB_WARM_TOKEN` enabled and ping `/warm-db` every 4 minutes on Neon free.
9. Leave the GitHub metadata cache TTLs enabled so archive listing and browsing reuse recent release/asset responses.
10. Keep `GITHUB_DRIVE_ARCHIVES_PAGE_SIZE` modest so the home page loads recent archives first and only fetches older pages on demand.

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

- Single Gunicorn worker, 4 threads by default. Task metadata is persisted to Postgres when configured, but the actual transfer worker still runs inside the web process, so keep `--workers` at 1 unless you move transfers to a dedicated queue.
- Web uploads and downloads default to 2 internal workers each, and the app can optionally serialize public traffic through `GITHUB_DRIVE_MAX_ACTIVE_TASKS_GLOBAL=1`.
- Release listings, release assets, manifests, and cover images are cached in-process for short TTLs to cut repeated GitHub API traffic during home-page refreshes and archive browsing.
- The home page loads archives in pages instead of traversing the entire releases history on every refresh; older archives are fetched on demand with "Load more".
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
| `GITHUB_DRIVE_TOKEN` | Optional CLI/operator PAT with `repo` scope |
| `GITHUB_DRIVE_REPO` | Optional CLI/operator target archives repo as `owner/repo` |
| `GITHUB_DRIVE_SESSION_SECRET` | Required by the web app for Flask sessions. Auto-generated if missing (warns). Also used as the legacy fallback for encryption key derivation when `GITHUB_DRIVE_ENCRYPTION_KEY` is not set. |
| `GITHUB_DRIVE_ENCRYPTION_KEY` | Stable AES key (hex or base64, 16/24/32 bytes). Survives redeploys, unlike the session secret. |
| `GITHUB_DRIVE_USER_ID` | Optional namespace for the derived encryption key (default `default`) |
| `GITHUB_DRIVE_ENCRYPT` | Set to `1`/`true` to encrypt all uploads from the web flow |
| `GITHUB_DRIVE_ENCRYPTION_KEY` | Optional CLI fallback if `--key` is not passed |
| `GITHUB_DRIVE_BASIC_AUTH` | `user:password` to gate the web app behind HTTP Basic |
| `GITHUB_DRIVE_ALLOW_SIGNUP` | `true`/`false`; disable on public hosted instances after provisioning users |
| `GITHUB_DRIVE_MIRROR_ENV_TOKEN` | Legacy opt-in to write env PATs to `token.json`; leave unset for multi-user hosting |
| `GITHUB_DRIVE_ENABLE_DB_CHECK` | Opt-in database diagnostic endpoint for signed-in users |
| `GITHUB_DRIVE_MAX_UPLOAD_BYTES` | Max bytes per upload request (default 5 GB) |
| `GITHUB_DRIVE_CHUNK_BYTES` | Per-chunk upload size when splitting large files (default ~1.9 GB) |
