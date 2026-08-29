# GitHub Drive for Android

A native Android client for [github-drive](https://github.com/harshivshah2504/storage-repo). Each
person signs in to **their own** GitHub account and their files go straight from the phone to their
own private repository as release assets. Nothing passes through the Render deployment — there is no
server in the middle to pay for, rate limit, or trust.

Archives written by this app are readable by the web app, and archives written by the web app are
readable here: the release tag, release-body marker, `_manifest.json` layout and asset naming are all
byte-identical to `github_drive/storage.py`.

---

## What works in this build

- **Sign in with GitHub** using the OAuth Device Flow — a short code, approved on github.com. No
  password typed into the app, no client secret shipped inside the APK.
- **Automatic setup.** On first sign-in the app creates a private `github-drive-archives` repo in the
  person's account. They are never asked to pick a repository or paste a token.
- **Upload** individual files, a whole folder (structure preserved), or anything shared into the app
  from another app via the Android share sheet.
- **Browse** archives in a cover-art grid, then walk the folder tree inside one.
- **Save** any file back to the device through the system file picker.
- **Delete** an archive (release plus git tag).
- **Background transfers** with a progress notification, so a large upload survives leaving the app.
- **Resume.** Re-uploading into an existing archive skips assets that are already there.
- **Chunking** for files over 1.9 GB, matching the Python chunk plan exactly.
- **Reads bundled archives.** Folders the web app auto-bundled into a single zip are browsable and
  downloadable here.

## Not in this build

- **Client-side encryption.** Encrypted archives are listed and browsable, but files cannot be
  decrypted on the phone. The `GDRV` envelope is fully specified and the port is straightforward —
  the wrinkle is that it stores the GCM tag *before* the ciphertext, where Java's `Cipher` expects it
  after, so both directions need splicing.
- **Auto-bundling on upload.** The phone always writes `file-assets` archives. A folder of 5,000 tiny
  files will therefore use 5,000 API requests rather than one; GitHub allows 5,000 authenticated
  requests an hour.
- **Adding files to an existing archive, deleting single files, creating empty folders.**

---

## One-time setup

### 1. Register a GitHub OAuth App

GitHub → Settings → Developer settings → **OAuth Apps** → New OAuth App.

| Field | Value |
|---|---|
| Application name | GitHub Drive |
| Homepage URL | your repo URL (anything valid) |
| Authorization callback URL | `https://github.com/login/device` (unused by device flow, but required) |

Then open the app's settings and tick **Enable Device Flow**. This is essential — without it GitHub
answers every sign-in with `device_flow_disabled`.

Copy the **Client ID**. It is public by design: the device flow uses no client secret, so nothing
sensitive ends up in the APK.

### 2. Tell the build about it

In the repository → Settings → Secrets and variables → Actions → **Variables** tab, add:

```
GH_OAUTH_CLIENT_ID = Ov23li...your client id
```

For local builds, put it in `android/gradle.properties` instead:

```properties
githubOauthClientId=Ov23li...your client id
```

### 3. (Optional) A signing key, so updates install over the top

Without this the APK is signed with the debug key. It installs fine, but a later build signed with a
different key cannot replace it — people have to uninstall first. To fix that permanently, generate a
keystore once:

```sh
keytool -genkeypair -v -keystore release.jks -keyalg RSA -keysize 2048 \
        -validity 10000 -alias githubdrive
base64 -w0 release.jks     # macOS: base64 -i release.jks
```

Add these repository **Secrets**:

| Secret | Value |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | the base64 blob printed above |
| `ANDROID_KEYSTORE_PASSWORD` | the keystore password |
| `ANDROID_KEY_ALIAS` | `githubdrive` |
| `ANDROID_KEY_PASSWORD` | the key password |

Keep `release.jks` somewhere safe and out of the repo — losing it means losing the ability to ship
updates to anyone who already installed.

---

## Building the APK

### On GitHub (no Android tooling needed)

Push this directory to the repo as `android/`, along with `.github/workflows/android.yml`. The
workflow runs on every push that touches `android/`, and can also be run by hand from the **Actions**
tab → *Build Android APK* → *Run workflow*.

The APK lands as a workflow artifact named `github-drive-apk`. Publishing a GitHub Release also
attaches `github-drive.apk` to it, which gives people a stable download link.

### Locally

Needs JDK 17 and the Android SDK (platform 35, build-tools 35.0.0):

```sh
cd android
gradle assembleRelease            # or: ./gradlew assembleRelease once a wrapper is generated
```

Output: `android/app/build/outputs/apk/release/app-release.apk`.

---

## Installing it

Android blocks APKs from outside the Play Store until the installing app is trusted. For a
non-technical person, the shortest honest instructions are:

1. Open the download link and tap the APK.
2. When Android says the browser isn't allowed to install apps, tap **Settings** → turn on **Allow
   from this source** → go back.
3. Tap **Install**, then **Open**.
4. Tap **Sign in with GitHub**, then **Open GitHub and approve**. The code is already copied and
   pre-filled; they tap **Continue**, then **Authorize**.
5. The app returns by itself and creates their storage repo.

---

## How the code is laid out

```
app/src/main/java/com/harshiv/githubdrive/
├── core/
│   ├── Format.kt        Wire format: tags, titles, release body, asset naming, classification
│   ├── PyJson.kt        JSON writer that matches CPython's json.dumps byte for byte
│   └── Prefs.kt         Settings; the token is wrapped with an Android Keystore AES/GCM key
├── github/
│   ├── GitHubClient.kt  The REST calls github-drive uses, with the same retry policy
│   └── DeviceFlow.kt    OAuth device flow
├── drive/
│   ├── Models.kt        Archive, entry and part models plus the folder-tree view
│   ├── DriveRepo.kt     Read side: list, browse, download, delete, bundle extraction
│   ├── Uploader.kt      Write side: release, chunk plan, assets, manifest, cover
│   ├── Picking.kt       Turns picker/share URIs into sorted upload items
│   └── Cover.kt         480x480 centre-cropped JPEG cover
├── transfer/            Foreground service + in-flight transfer state
└── ui/                  Compose screens
```

### The parts that are easy to get wrong

These were checked against the Python line by line, and a test harness diffs the Kotlin output
against the real `storage.py` functions:

- **`created_at` is `...+00:00`, never `Z`.** Java's default ISO formatter produces `Z`, which the
  web app would not round-trip.
- **`_manifest.json` is `json.dumps(..., indent=2)`,** which means `": "` after keys, `,` before each
  newline, empty containers inline as `[]`/`{}`, non-ASCII escaped as `\uXXXX`, and no trailing
  newline. The release body is the compact `,`/`:` form of the same encoder.
- **Key order is load-bearing** in both the manifest and the release body, so both are built with
  `LinkedHashMap` in the exact order the Python dataclasses emit.
- **Asset names** flatten `/` to `__` *before* sanitising, collapse each run of unsafe characters to a
  single `-`, strip leading and trailing `-` and `.`, and truncate to 180 chars (160 for chunked
  parts, leaving room for `.partNNNN`).
- **Entry order** sorts on the whole `/`-separated path string, not segment by segment — Python's
  `sorted(Path.glob(...))` compares the full string, and the two orders differ.
- **`archive_id` is uppercase in metadata but lowercase in the tag.**
- **Archives are found by the `GITHUB_DRIVE_ARCHIVE=` marker in the release body,** not by the tag
  prefix.

## Limits worth knowing

- GitHub caps a release asset at **2 GB**; the chunk budget is 1,900,000,000 bytes to stay under it.
- Authenticated REST is **5,000 requests/hour**. One file is roughly one request, so a very large
  folder can hit that ceiling — which is what auto-bundling exists to solve on the web side.
- Private repos have no published storage cap, but they are not unlimited in practice. Very large
  libraries are a good way to attract GitHub's attention.
