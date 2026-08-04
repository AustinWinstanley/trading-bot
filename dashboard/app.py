"""Flask app factory for the read-only monitoring dashboard."""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from .converters import ProfileConverter

_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT_DEFAULT = _PACKAGE_ROOT.parent


def create_app(repo_root: Path | str | None = None) -> Flask:
    """Build the Flask app. `repo_root` is explicit (not hardcoded at import
    time) so tests can point it at a temp fixture directory."""
    app = Flask(__name__)
    app.url_map.converters["profile"] = ProfileConverter
    app.config["REPO_ROOT"] = Path(repo_root) if repo_root is not None else _REPO_ROOT_DEFAULT

    from .routes import bp
    app.register_blueprint(bp)
    return app
