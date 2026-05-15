"""ai_guard_tracker.py — Cable Scalp v2.0 (No-AI edition)

The AI guard tracker feature has been removed in v2.0.
This module is a transparent no-op stub that keeps every import and every
call-site in bot.py (backfill_pnl, _execution_phase) fully compatible.

None of the stub functions perform any I/O, file writes, or network calls.
They all return safe, typed values that bot.py already guards against.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Public API — mirrors the v1.x signatures exactly
# ---------------------------------------------------------------------------

def record_ai_decision(
    settings: "dict[str, Any]" = None,          # noqa: ARG001
    payload:  "dict[str, Any]" = None,           # noqa: ARG001
    ai_result: "dict[str, Any]" = None,          # noqa: ARG001
    entry: float = 0.0,                          # noqa: ARG001
    sl_price: float = 0.0,                       # noqa: ARG001
    tp_price: float = 0.0,                       # noqa: ARG001
    estimated_risk_usd: float = 0.0,             # noqa: ARG001
    estimated_reward_usd: float = 0.0,           # noqa: ARG001
    sl_pips: int = 0,                            # noqa: ARG001
    tp_pips: int = 0,                            # noqa: ARG001
    **kwargs: Any,
) -> None:
    """No-op — decision tracking removed in v2.0.

    Returns None; bot.py no longer checks this value in v2.0.
    """
    return None


def link_trade(
    decision_id: "str | None",   # noqa: ARG001
    trade_id: "str | None",      # noqa: ARG001
) -> None:
    """No-op — AI trade linking removed in v2.0."""
    return None


def mark_trade_failed(
    decision_id: "str | None",   # noqa: ARG001
    error: str = "",              # noqa: ARG001
) -> None:
    """No-op — AI failure marking removed in v2.0."""
    return None


def backfill_actual_trade_result(
    trade_id: str,               # noqa: ARG001
    pnl: float,                  # noqa: ARG001
    closed_at_sgt: "str | None" = None,  # noqa: ARG001
) -> None:
    """No-op — AI result back-fill removed in v2.0."""
    return None


def update_blocked_virtual_outcomes(
    trader: Any = None,          # noqa: ARG001
    instrument: str = "",        # noqa: ARG001
    now_sgt: Any = None,         # noqa: ARG001
) -> dict:
    """No-op — virtual outcome tracking removed in v2.0.

    Returns an empty dict so bot.py's ``if _ai_virtual.get("updated") or ...``
    guard evaluates to False without raising AttributeError.
    """
    return {}
