"""URL converter restricting the profile path segment to known values."""

from __future__ import annotations

from werkzeug.routing import BaseConverter


class ProfileConverter(BaseConverter):
    """Matches exactly 'base' or '2x' — anything else 404s at routing time."""

    regex = r"base|2x"
