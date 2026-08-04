"""Read-only monitoring dashboard.

This package never imports engine.execute, engine.data, scripts.run_daily,
or scripts.healthcheck — those transitively touch the Alpaca client. It only
reads local SQLite journals (in genuine read-only mode) and JSON state files
already written by the cron jobs. See the module docstrings in db.py and
routes.py for the specifics.
"""
