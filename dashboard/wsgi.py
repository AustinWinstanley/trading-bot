"""gunicorn entrypoint: `gunicorn dashboard.wsgi:app`."""

from .app import create_app

app = create_app()
