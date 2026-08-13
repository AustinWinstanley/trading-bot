"""Tests for scripts/options_daily.py's buying-power pre-flight check and
consecutive-miss escalation — added after a real incident (2026-08-13):
the options gate only checked the experiment's own allocation_pct budget,
never the account's actual options_buying_power, so a structure the gate
approved could still be rejected by Alpaca after leaving the door.
"""

from __future__ import annotations

from scripts.options_daily import (
    BUYING_POWER_MISS_ESCALATION,
    EXPERIMENT_NAME,
    _buying_power_miss_message,
    _buying_power_shortfall,
)


class TestBuyingPowerShortfall:
    def test_unknown_buying_power_defers_to_alpaca(self):
        """None (field absent from the account payload) must never block
        on a guess — Alpaca's own rejection remains the backstop."""
        assert _buying_power_shortfall(None, required=443.0) is False

    def test_sufficient_buying_power_is_not_a_shortfall(self):
        assert _buying_power_shortfall(500.0, required=443.0) is False

    def test_exactly_enough_is_not_a_shortfall(self):
        assert _buying_power_shortfall(443.0, required=443.0) is False

    def test_insufficient_buying_power_is_a_shortfall(self):
        assert _buying_power_shortfall(374.14, required=443.0) is True


class TestBuyingPowerMissMessage:
    def test_first_miss_is_plain_not_critical(self):
        misses = {}
        msg = _buying_power_miss_message(misses, "REJECT SPY: insufficient")
        assert "CRITICAL" not in msg
        assert misses[EXPERIMENT_NAME] == 1

    def test_counter_increments_across_calls(self):
        misses = {}
        _buying_power_miss_message(misses, "a")
        _buying_power_miss_message(misses, "b")
        assert misses[EXPERIMENT_NAME] == 2

    def test_escalates_to_critical_at_threshold(self):
        misses = {EXPERIMENT_NAME: BUYING_POWER_MISS_ESCALATION - 1}
        msg = _buying_power_miss_message(misses, "REJECT SPY: insufficient")
        assert msg.startswith("CRITICAL:")
        assert misses[EXPERIMENT_NAME] == BUYING_POWER_MISS_ESCALATION

    def test_stays_critical_beyond_threshold(self):
        misses = {EXPERIMENT_NAME: BUYING_POWER_MISS_ESCALATION + 5}
        msg = _buying_power_miss_message(misses, "REJECT SPY: insufficient")
        assert msg.startswith("CRITICAL:")

    def test_other_experiments_tracked_independently(self):
        misses = {"some_other_experiment": 10}
        _buying_power_miss_message(misses, "REJECT SPY: insufficient")
        assert misses[EXPERIMENT_NAME] == 1
        assert misses["some_other_experiment"] == 10

    def test_resetting_to_zero_then_a_fresh_miss_starts_over(self):
        """Mirrors the real flow: a successful open sets
        buying_power_misses[EXPERIMENT_NAME] = 0 in scripts/options_daily.py's
        main(); a later miss should not remember the prior streak."""
        misses = {EXPERIMENT_NAME: BUYING_POWER_MISS_ESCALATION}
        misses[EXPERIMENT_NAME] = 0  # simulated successful open
        msg = _buying_power_miss_message(misses, "REJECT SPY: insufficient")
        assert "CRITICAL" not in msg
        assert misses[EXPERIMENT_NAME] == 1
