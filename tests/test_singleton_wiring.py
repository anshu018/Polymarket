"""
tests/test_singleton_wiring.py — Step 3 (B4) main.py wiring contract.

The singleton guard's BEHAVIOR (claim / wait-then-exit / renewer semantics)
is proven in tests/test_lease.py against coordinator.lease directly. This
module locks the WIRING in main.py: the __singleton__ claim must run at
startup BEFORE startup reconciliation and before any trading task is
created — two overlapping instances must never reconcile or trade
concurrently — and the renewer task must be registered.
"""

import ast
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def _main_call_lines() -> dict[str, int]:
    """First call site (by line) of every named function inside main()."""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    main_fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"
    )
    lines: dict[str, int] = {}
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                lines.setdefault(node.func.id, node.lineno)
            elif isinstance(node.func, ast.Attribute):
                lines.setdefault(node.func.attr, node.lineno)
    return lines


def _line_of(calls: dict[str, int], name: str) -> int:
    assert name in calls, f"main.py no longer calls {name}() — wiring changed"
    return calls[name]


def test_singleton_claim_precedes_reconciliation_and_trading_tasks() -> None:
    calls = _main_call_lines()
    claim = _line_of(calls, "acquire_singleton_or_exit")
    # Reconciliation mutates position state — two instances must never run
    # it concurrently, so the guard is claimed first.
    assert claim < _line_of(calls, "reconcile_on_startup")
    # No trading task may start before the instance is proven sole.
    for task in ("run_copy_poller", "run_class_a_executor", "run_class_b_executor"):
        assert claim < _line_of(calls, task)


def test_singleton_renewer_task_registered() -> None:
    """A claimed guard must be kept alive — the renewer task joins the
    supervised set (E-B13); without it the lease expires mid-run and a
    second instance could claim."""
    calls = _main_call_lines()
    renewer = _line_of(calls, "run_singleton_lease_renewer")
    assert renewer > _line_of(calls, "acquire_singleton_or_exit"), (
        "the renewer must start only after the claim succeeded"
    )
