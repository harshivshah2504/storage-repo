"""Entry point for Render's default `gunicorn app:app` command."""
from github_drive.webapp import create_app

app = create_app()
