"""Config.holding_sleeve — a held position's journal sleeve reduced to the
sleeves that still hold it. See AGENTS.md ("A stood-down sleeve kept
deciding whether the SPY core carries a stop")."""

from __future__ import annotations

import dataclasses

import pytest

from engine.config import load_config


def _cfg(**weights):
    cfg = load_config()
    paper = {**cfg.sleeves_paper, "sleeves": weights}
    return dataclasses.replace(cfg, sleeves_paper=paper)


@pytest.mark.parametrize("journaled, holding", [
    ("equity_core+trend", "equity_core"),     # the live 2x SPY case
    ("trend+equity_core", "equity_core"),
    ("equity_core", "equity_core"),
    ("equity_core+lev_trend", "equity_core+lev_trend"),  # both live: unchanged
    # Every part stood down: still the position that sleeve opened.
    ("mom_ls", "mom_ls"),
    ("trend+mom_ls", "trend+mom_ls"),
    # Not a portfolio sleeve at all (an experiment, a pseudo-sleeve): kept.
    ("mom_ls+bull_put_delta_selected_live", "bull_put_delta_selected_live"),
    ("rebalance", "rebalance"),
    ("", ""),
])
def test_holding_sleeve(journaled, holding):
    cfg = _cfg(equity_core=0.8, lev_trend=0.2, trend=0.0, tsmom=0.0, mom_ls=0.0)
    assert cfg.holding_sleeve(journaled) == holding


def test_a_combined_sleeve_with_a_live_stopped_part_still_gets_a_stop():
    # trend allocated again: the combined position is part stopped sleeve,
    # so exact-match exemption must keep saying "stops apply".
    cfg = _cfg(equity_core=0.6, trend=0.2, lev_trend=0.2)
    sleeve = cfg.holding_sleeve("equity_core+trend")
    assert sleeve == "equity_core+trend"
    assert cfg.risk.stops_apply_to(sleeve)


def test_shipped_configs_exempt_the_spy_core():
    cfg = load_config()
    assert not cfg.risk.stops_apply_to(cfg.holding_sleeve("equity_core+trend"))
