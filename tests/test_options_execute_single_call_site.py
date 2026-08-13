"""Static guarantee: Trader.submit_multi_leg_order — the only place a real
multi-leg options order can be constructed and sent to the broker — is
never called from anywhere except scripts/options_daily.py.

tests/test_shadow_read_only.py already proves the read-only shadow modules
never call it (it is in that test's FORBIDDEN_METHOD_CALLS set). This test
proves the complementary, broader claim: no OTHER file in the repo calls it
either — a stray one-off script or a future shadow variant gaining this
capability by accident would fail this test, not slip through unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIRS = (REPO_ROOT / "engine", REPO_ROOT / "scripts")
ALLOWED_CALLER = REPO_ROOT / "scripts" / "options_daily.py"
METHOD_NAME = "submit_multi_leg_order"


def _call_sites(py_file: Path) -> list[int]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == METHOD_NAME
        ):
            lines.append(node.lineno)
    return lines


def test_submit_multi_leg_order_only_called_from_options_daily():
    offenders = {}
    allowed_call_count = 0
    for directory in SEARCH_DIRS:
        for py_file in sorted(directory.glob("*.py")):
            sites = _call_sites(py_file)
            if not sites:
                continue
            if py_file == ALLOWED_CALLER:
                allowed_call_count += len(sites)
                continue
            offenders[str(py_file.relative_to(REPO_ROOT))] = sites
    assert not offenders, (
        f"submit_multi_leg_order called outside {ALLOWED_CALLER.name}: {offenders} — "
        "real order submission must stay confined to the one script that gates "
        "every call through engine.options_risk.evaluate_option_structure first"
    )
    # Sanity check the test itself isn't vacuous — options_daily.py's own
    # open and close paths both call it.
    assert allowed_call_count >= 1


def test_the_method_itself_still_exists_on_trader():
    """If this method is ever renamed, the above test would otherwise pass
    vacuously (zero call sites anywhere) and silently stop proving anything."""
    from engine.execute import Trader

    assert hasattr(Trader, "submit_multi_leg_order")
