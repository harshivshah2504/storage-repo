import argparse
import os
import sys

from .api import GitHubClient, parse_owner_repo
from .auth_manager import (
    auth_status,
    generate_encryption_key,
    get_client,
    get_encryption_key,
    get_token,
    save_token,
)
from .storage import (
    delete_archive,
    download_archive,
    list_remote_archives,
    upload_archive,
)
from .webapp import run_web


def cmd_upload(args):
    resume_targets = [value for value in (args.resume_release_id, args.resume_tag, args.resume_archive_id) if value]
    if len(resume_targets) > 1:
        raise RuntimeError("Choose only one resume target: --resume-release-id, --resume-tag, or --resume-archive-id.")
    encode_key = _resolve_encode_key(args)
    manifest = upload_archive(
        source_path=args.source,
        private_release=args.private_release,
        workers=args.workers,
        recursive=not args.no_recursive,
        retries=args.retries,
        encrypt=args.encrypt,
        encode_key=encode_key,
        resume_release_id=args.resume_release_id,
        resume_tag=args.resume_tag,
        resume_archive_id=args.resume_archive_id,
    )
    print(f"Archive ID: {manifest.archive_id}")
    print(f"Release tag: {manifest.tag}")
    if manifest.html_url:
        print(f"Release URL: {manifest.html_url}")
    print(f"Files uploaded: {manifest.total_items}")


def cmd_download(args):
    encode_key = _resolve_encode_key(args)
    result = download_archive(
        release_id=args.release_id,
        tag=args.tag,
        archive_id=args.archive_id,
        destination_dir=args.destination,
        workers=args.workers,
        skip_existing=not args.overwrite_existing,
        retries=args.retries,
        encode_key=encode_key,
    )
    print(f"Downloaded {result['downloaded_items']} item(s) to {result['destination_dir']}")


def cmd_list(_args):
    archives = list_remote_archives()
    if not archives:
        print("No GitHub Drive archives were found.")
        return
    for archive in archives:
        meta = archive.get("archive", {})
        print(
            f"{archive['tag']} | {meta.get('source_name', archive.get('name', ''))} | "
            f"{meta.get('total_items', archive.get('asset_count', 0))} item(s) | "
            f"created {meta.get('created_at', '')} | release {archive['release_id']}"
        )


def cmd_delete(args):
    result = delete_archive(
        release_id=args.release_id,
        tag=args.tag,
        archive_id=args.archive_id,
        delete_tag=not args.keep_tag,
    )
    print(f"Deleted release {result['release_id']} (tag {result['tag'] or 'n/a'}).")


def cmd_auth(args):
    token = (args.token or os.environ.get("GITHUB_DRIVE_TOKEN") or "").strip()
    if not token:
        token = (input("Paste your GitHub Personal Access Token: ").strip())
    if not token:
        print("No token provided.", file=sys.stderr)
        sys.exit(2)

    owner, repo = ("", "")
    if args.repo:
        owner, repo = parse_owner_repo(args.repo)

    client = GitHubClient(token=token, owner=owner or "octocat", repo=repo or "octocat")
    login = client.viewer_login()
    print(f"Authenticated as {login}.")

    if args.repo:
        owner, repo = parse_owner_repo(args.repo)
        if args.create_repo:
            scoped = GitHubClient(token=token, owner=owner, repo=repo)
            scoped.ensure_repo(private=args.private_repo)
            print(f"Repository {owner}/{repo} is ready.")
        save_token(token, owner=owner, repo=repo)
        print(f"Saved token and repository ({owner}/{repo}).")
    else:
        save_token(token)
        print("Saved token. Set --repo or GITHUB_DRIVE_REPO before running upload/download.")


def cmd_auth_status(_args):
    status = auth_status()
    print(f"Token present: {status['token_present']} (source: {status['token_source']})")
    print(f"Repository: {status['repo'] or '(unset)'} (source: {status['repo_source']})")
    print(f"Token file: {status['token_file']}")


def cmd_web(args):
    run_web(host=args.host, port=args.port, open_browser=not args.no_browser)


def cmd_gui(_args):
    run_web()


def _resolve_encode_key(args):
    """Resolve the AES key for the CLI.

    Order of precedence:
      1. --key <passphrase> on the CLI (utf-8 padded/truncated to 16 bytes; for ad-hoc tests).
      2. GITHUB_DRIVE_ENCRYPTION_KEY env var (hex/base64, recommended).
      3. GITHUB_DRIVE_SESSION_SECRET (legacy fallback).
    Returns None if no encryption was requested.
    """
    encrypt = getattr(args, "encrypt", False)
    decrypt = getattr(args, "decrypt", False)
    raw_passphrase = getattr(args, "key", None)
    if not (encrypt or decrypt or raw_passphrase):
        return None
    if raw_passphrase:
        key_bytes = raw_passphrase.encode("utf-8")
        if len(key_bytes) < 16:
            key_bytes = key_bytes.ljust(16, b"0")
        return key_bytes[:16]
    return get_encryption_key()


def cmd_users_list(_args):
    from . import users

    rows = users.list_users()
    if not rows:
        print("No users registered. Add one with: github-drive users add <username>")
        return
    print(f"{'username':<24} {'created_at':<26} {'github_repo'}")
    for row in rows:
        print(f"{row['username']:<24} {row.get('created_at','')[:26]:<26} {row.get('repo','') or '(unset)'}")


def cmd_users_add(args):
    from . import users
    import getpass

    password = args.password
    if not password:
        password = getpass.getpass(f"Password for {args.username}: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            sys.exit(2)
    try:
        record = users.create_user(args.username, password)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Created user {record['username']!r}.")
    print(
        "They can now sign in at the web URL. They will be prompted to enter their own GitHub PAT and target repo on first use."
    )


def cmd_users_remove(args):
    from . import users

    if users.delete_user(args.username):
        print(f"Removed user {args.username!r}.")
    else:
        print(f"User {args.username!r} not found.", file=sys.stderr)
        sys.exit(2)


def cmd_users_set_password(args):
    from . import users
    import getpass

    password = args.password
    if not password:
        password = getpass.getpass(f"New password for {args.username}: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            sys.exit(2)
    try:
        users.change_password(args.username, password)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Updated password for {args.username!r}.")


def cmd_gen_key(args):
    if args.from_legacy:
        from .auth_manager import derive_encode_key
        key = derive_encode_key(args.user_id)
        print(key.hex())
        print(
            f"# Hex of the legacy key derived from GITHUB_DRIVE_SESSION_SECRET + user_id={args.user_id!r}.\n"
            "# Set this as GITHUB_DRIVE_ENCRYPTION_KEY to keep decrypting archives created before migration.",
            file=sys.stderr,
        )
        return
    key_hex = generate_encryption_key(num_bytes=args.bytes)
    print(key_hex)
    print(
        f"# {args.bytes * 8}-bit AES key. Save it as GITHUB_DRIVE_ENCRYPTION_KEY in your platform's "
        "secret manager. Treat it like a password — anyone with this key can decrypt your archives.",
        file=sys.stderr,
    )


def build_parser():
    parser = argparse.ArgumentParser("github-drive")
    subparsers = parser.add_subparsers(title="commands")

    upload_parser = subparsers.add_parser(
        "upload",
        aliases=["up"],
        help="upload a file or folder as a GitHub Release archive",
    )
    upload_parser.add_argument("source", help="source file or folder")
    upload_parser.add_argument("--workers", type=int, default=4, help="concurrent uploads")
    upload_parser.add_argument("--no-recursive", action="store_true", help="folder mode: skip subdirectories")
    upload_parser.add_argument("--retries", type=int, default=3, help="retry attempts per call")
    upload_parser.add_argument("--private-release", action="store_true", help="mark release as prerelease")
    upload_parser.add_argument("--encrypt", action="store_true", help="encrypt files with AES-128-GCM before upload")
    upload_parser.add_argument("--key", default=None, help="encryption key (passphrase, padded/truncated to 16 bytes)")
    upload_parser.add_argument("--resume-release-id", type=int, default=None, help="resume uploading into an existing release id")
    upload_parser.add_argument("--resume-tag", default=None, help="resume uploading into an existing release tag")
    upload_parser.add_argument("--resume-archive-id", default=None, help="resume uploading into an existing archive id")
    upload_parser.set_defaults(handle=cmd_upload)

    download_parser = subparsers.add_parser(
        "download",
        aliases=["down", "dl"],
        help="download an archive from a GitHub Release into a folder",
    )
    download_parser.add_argument("--release-id", type=int, default=None, help="GitHub release id")
    download_parser.add_argument("--tag", default=None, help="GitHub release tag (e.g. github-drive-XXXXXX)")
    download_parser.add_argument("--archive-id", default=None, help="github-drive archive id")
    download_parser.add_argument("destination", help="local destination folder")
    download_parser.add_argument("--workers", type=int, default=4, help="concurrent downloads")
    download_parser.add_argument("--retries", type=int, default=3, help="retry attempts per call")
    download_parser.add_argument("--overwrite-existing", action="store_true", help="redownload files already present")
    download_parser.add_argument("--decrypt", action="store_true", help="archive is encrypted; require key")
    download_parser.add_argument("--key", default=None, help="encryption key used at upload time")
    download_parser.set_defaults(handle=cmd_download)

    list_parser = subparsers.add_parser("list", help="list github-drive archives in the configured repository")
    list_parser.set_defaults(handle=cmd_list)

    delete_parser = subparsers.add_parser("delete", help="delete an archive (release) and optionally its tag")
    delete_parser.add_argument("--release-id", type=int, default=None)
    delete_parser.add_argument("--tag", default=None)
    delete_parser.add_argument("--archive-id", default=None)
    delete_parser.add_argument("--keep-tag", action="store_true", help="do not delete the underlying git tag")
    delete_parser.set_defaults(handle=cmd_delete)

    auth_parser = subparsers.add_parser("auth", help="store a GitHub Personal Access Token and target repository")
    auth_parser.add_argument("--token", default=None, help="GitHub PAT (otherwise read from stdin or env)")
    auth_parser.add_argument("--repo", default=None, help="target repository as owner/repo")
    auth_parser.add_argument("--create-repo", action="store_true", help="create the repository if it does not exist")
    auth_parser.add_argument("--private-repo", action="store_true", default=True, help="create the repo as private")
    auth_parser.set_defaults(handle=cmd_auth)

    auth_status_parser = subparsers.add_parser("auth-status", help="show stored credentials and repository")
    auth_status_parser.set_defaults(handle=cmd_auth_status)

    gen_key_parser = subparsers.add_parser(
        "gen-key",
        help="generate a stable encryption key for GITHUB_DRIVE_ENCRYPTION_KEY",
    )
    gen_key_parser.add_argument(
        "--bytes", type=int, choices=(16, 24, 32), default=32,
        help="key length in bytes (16=AES-128, 24=AES-192, 32=AES-256). Default 32.",
    )
    gen_key_parser.add_argument(
        "--from-legacy", action="store_true",
        help="emit the key derived from GITHUB_DRIVE_SESSION_SECRET so you can migrate without losing prior archives",
    )
    gen_key_parser.add_argument(
        "--user-id", default="default",
        help="user id used in the legacy derivation (matches GITHUB_DRIVE_USER_ID at the time of upload)",
    )
    gen_key_parser.set_defaults(handle=cmd_gen_key)

    web_parser = subparsers.add_parser("web", help="launch the localhost web frontend")
    web_parser.add_argument("--host", default="127.0.0.1", help="host interface to bind")
    web_parser.add_argument("--port", default=8765, type=int, help="port to listen on")
    web_parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    web_parser.set_defaults(handle=cmd_web)

    gui_parser = subparsers.add_parser("gui", help="alias for the web frontend")
    gui_parser.set_defaults(handle=cmd_gui)

    users_parser = subparsers.add_parser(
        "users",
        help="manage web app accounts (multi-tenant hosting only)",
    )
    users_sub = users_parser.add_subparsers(title="users commands")

    users_list = users_sub.add_parser("list", help="list registered web app users")
    users_list.set_defaults(handle=cmd_users_list)

    users_add = users_sub.add_parser("add", help="create a new web app user")
    users_add.add_argument("username", help="username (2-32 chars, lowercase letters/digits/dot/underscore/hyphen)")
    users_add.add_argument("--password", default=None, help="password (otherwise prompted)")
    users_add.set_defaults(handle=cmd_users_add)

    users_remove = users_sub.add_parser("remove", help="delete a web app user (keeps GitHub repo intact)")
    users_remove.add_argument("username")
    users_remove.set_defaults(handle=cmd_users_remove)

    users_passwd = users_sub.add_parser("set-password", help="change a user's password")
    users_passwd.add_argument("username")
    users_passwd.add_argument("--password", default=None, help="new password (otherwise prompted)")
    users_passwd.set_defaults(handle=cmd_users_set_password)

    users_parser.set_defaults(handle=lambda _a: users_parser.print_help())

    return parser


def main(args):
    parser = build_parser()
    arguments = parser.parse_args(args)
    if not hasattr(arguments, "handle"):
        parser.print_help()
        sys.exit(1)
    arguments.handle(arguments)


def run():
    main(sys.argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
