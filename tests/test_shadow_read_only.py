"""Every options/intraday research collector claims, in its own docstring
and in the committed launch/decision JSONs, that it cannot submit, cancel,
or otherwise mutate a broker order — see e.g. options_shadow.py's module
docstring ("No method in this module submits, replaces, cancels,
exercises, or otherwise mutates a broker account") and
reports/zero_dte_shadow_launch.json's "safety" list. That claim was
previously verified only by manual inspection (an earlier code audit did
it by reading every file). This makes it a static, permanent, automatically
re-checked property instead — mirroring tests/dashboard/test_safety.py's
AST-based approach for the dashboard's own architectural-isolation claim.

Unlike the dashboard, these scripts DO legitimately import engine.execute.
Trader — they need read access to quotes/prices/positions. What must never
appear is a CALL to any of Trader's order-mutating methods. _post/_delete
are the two low-level primitives every write method funnels through
(engine/execute.py), so forbidding them alone is already sufficient; the
higher-level names are included too so a violation is caught however it's
written and the intent stays legible in a failure message.
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

SHADOW_MODULES = (
    "options_shadow.py",
    "momentum_options_shadow.py",
    "event_volatility_shadow.py",
    "zero_dte_shadow.py",
    "intraday_options_shadow.py",
    "execution_timing.py",
)

FORBIDDEN_METHOD_CALLS = frozenset({
    "_post",
    "_delete",
    "cancel_order",
    "cancel_all_orders",
    "submit_limit",
    "submit_protected_limit",
    "submit_entry",
    "submit_multi_leg_order",
})


def _called_method_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_shadow_modules_never_call_an_order_mutating_method():
    checked = 0
    for filename in SHADOW_MODULES:
        py_file = SCRIPTS_DIR / filename
        assert py_file.exists(), f"expected {py_file} to exist"
        called = _called_method_names(py_file)
        offending = called & FORBIDDEN_METHOD_CALLS
        assert not offending, (
            f"{filename} calls order-mutating method(s) {offending} — "
            "this module's docstring/launch JSON claims read-only"
        )
        checked += 1
    assert checked == len(SHADOW_MODULES)


def test_forbidden_method_list_matches_traders_actual_write_methods():
    """If engine/execute.py grows a new write method, this test should be
    the thing that fails, not a silent gap in FORBIDDEN_METHOD_CALLS."""
    import ast as _ast

    execute_py = SCRIPTS_DIR.parent / "engine" / "execute.py"
    tree = _ast.parse(execute_py.read_text())
    class_node = next(
        node for node in _ast.walk(tree)
        if isinstance(node, _ast.ClassDef) and node.name == "Trader"
    )
    read_only = {"clock", "latest_price", "open_orders", "get_order"}
    method_names = {
        node.name for node in class_node.body if isinstance(node, _ast.FunctionDef)
    }
    write_methods = method_names - read_only
    assert write_methods == set(FORBIDDEN_METHOD_CALLS), (
        f"engine.execute.Trader's methods {method_names} no longer match "
        f"this test's read_only={read_only} / forbidden={FORBIDDEN_METHOD_CALLS} "
        "split — update whichever side is stale"
    )
