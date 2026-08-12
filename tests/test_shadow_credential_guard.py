"""Every 2x-profile shadow script must stand down loudly when the 2x
credentials are absent, rather than falling through to Trader()'s
os.environ.get(...) fallback onto the unsuffixed (base) key/secret —
engine.data.AlpacaClient.__init__ does `key or os.environ.get("ALPACA_API_KEY", "")`,
so a None key silently authenticates as the base account and would record
"profile": "2x" rows sourced from it.

Each test monkeypatches just enough to reach the credential-guard line
deterministically (bypassing real .env/state-file/date dependencies) and
replaces Trader with a stub that raises if constructed — proving the guard
short-circuits BEFORE any client is built, not just before an API call
happens to fail.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import scripts.event_volatility_shadow as event_volatility_shadow
import scripts.momentum_options_shadow as momentum_options_shadow
import scripts.options_shadow as options_shadow
import scripts.zero_dte_shadow as zero_dte_shadow

ET = ZoneInfo("America/New_York")


class _TraderMustNotBeConstructed:
    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "Trader() was constructed despite missing 2x credentials — "
            "the guard did not fire before reaching it"
        )


def _clear_2x_creds(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_2X", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_2X", raising=False)
    # Base creds present but must never be used for a 2x-profile run.
    monkeypatch.setenv("ALPACA_API_KEY", "base-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "base-secret")


def test_options_shadow_stands_down_without_2x_credentials(monkeypatch, capsys):
    _clear_2x_creds(monkeypatch)
    monkeypatch.setattr(options_shadow, "load_env", lambda: None)
    monkeypatch.setattr(options_shadow, "Trader", _TraderMustNotBeConstructed)
    monkeypatch.setattr(
        options_shadow, "load_config",
        lambda _path: SimpleNamespace(sleeves_paper={
            "options_experiments": {"bull_put_fixed_width": {"mode": "shadow"}},
        }),
    )
    monkeypatch.setattr(sys, "argv", ["options_shadow", "--profile", "2x"])

    with pytest.raises(SystemExit) as exc:
        options_shadow.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "CRITICAL" in out
    assert "ALPACA_API_KEY_2X" in out


def test_momentum_options_shadow_stands_down_without_2x_credentials(monkeypatch, capsys, tmp_path):
    _clear_2x_creds(monkeypatch)
    targets_file = "state/mom_ls_targets.json"
    (tmp_path / "state").mkdir()
    (tmp_path / targets_file).write_text(
        json.dumps({"as_of": dt.datetime.now(ET).date().isoformat()})
    )
    monkeypatch.setattr(momentum_options_shadow, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(momentum_options_shadow, "load_env", lambda: None)
    monkeypatch.setattr(momentum_options_shadow, "Trader", _TraderMustNotBeConstructed)
    monkeypatch.setattr(
        momentum_options_shadow, "load_config",
        lambda _path: SimpleNamespace(sleeves_paper={
            "options_experiments": {"momentum_verticals": {"mode": "shadow"}},
            "mom_ls_targets_file": targets_file,
            "mom_ls_max_age_days": 10,
        }),
    )
    monkeypatch.setattr(sys, "argv", ["momentum_options_shadow", "--profile", "2x"])

    with pytest.raises(SystemExit) as exc:
        momentum_options_shadow.main()
    assert exc.value.code == 1
    assert "CRITICAL" in capsys.readouterr().out


def test_event_volatility_shadow_stands_down_without_2x_credentials(monkeypatch, capsys):
    _clear_2x_creds(monkeypatch)
    today = dt.datetime.now(ET).date()
    monkeypatch.setattr(event_volatility_shadow, "load_env", lambda: None)
    monkeypatch.setattr(event_volatility_shadow, "Trader", _TraderMustNotBeConstructed)
    monkeypatch.setattr(
        event_volatility_shadow, "load_config",
        lambda _path: SimpleNamespace(sleeves_paper={
            "options_experiments": {"event_volatility": {
                "mode": "shadow",
                "observation_window_days": 5,
                "events": [{"name": "TEST_EVENT", "date": today.isoformat()}],
            }},
        }),
    )
    monkeypatch.setattr(sys, "argv", ["event_volatility_shadow", "--profile", "2x"])

    with pytest.raises(SystemExit) as exc:
        event_volatility_shadow.main()
    assert exc.value.code == 1
    assert "CRITICAL" in capsys.readouterr().out


def test_zero_dte_shadow_stands_down_without_2x_credentials(monkeypatch, capsys):
    _clear_2x_creds(monkeypatch)
    monkeypatch.setattr(zero_dte_shadow, "load_env", lambda: None)
    monkeypatch.setattr(zero_dte_shadow, "Trader", _TraderMustNotBeConstructed)
    monkeypatch.setattr(
        zero_dte_shadow, "load_config",
        lambda _path: SimpleNamespace(sleeves_paper={
            "options_experiments": {"zero_dte_surface": {"mode": "shadow"}},
        }),
    )
    monkeypatch.setattr(sys, "argv", ["zero_dte_shadow", "--profile", "2x"])

    with pytest.raises(SystemExit) as exc:
        zero_dte_shadow.main()
    assert exc.value.code == 1
    assert "CRITICAL" in capsys.readouterr().out
