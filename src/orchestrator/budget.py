"""Token counter — display only, no enforcement.

This runs against a local model, so there are no token budgets and the loop
never pauses for token usage. We still COUNT tokens (per cycle and per day) so
the dashboard can show throughput/usage, and the runner feeds usage here from
OpenCode's JSON event stream. But ``ok`` is always True and ``breached`` is
always empty — nothing in the controller gates on token count.

The real safety net for 24/7 operation is in guardrails.py (loop detector,
no-progress detector, kill switch) and git_ops.py (auto-revert on failed verify).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db import add_tokens, tokens_for_day

log = logging.getLogger("solo.tokens")


@dataclass
class TokenCounter:
    """Tracks token usage for display. Never blocks the loop."""

    cycle_tokens: int = 0
    day: str = ""
    day_tokens: int = 0

    def reset_cycle(self) -> None:
        self.cycle_tokens = 0

    def rollover_day_if_needed(self) -> None:
        from datetime import date

        today = date.today().isoformat()
        if self.day != today:
            self.day = today
            self.day_tokens = tokens_for_day(today)

    def add(self, tokens: int) -> None:
        """Record tokens used this cycle + today. Display only — never blocks."""
        if tokens <= 0:
            return
        self.rollover_day_if_needed()
        self.cycle_tokens += tokens
        self.day_tokens = add_tokens(self.day, tokens)

    @property
    def ok(self) -> bool:
        # Always True — no budgets on a local model. The loop runs 24/7.
        return True

    @property
    def breached(self) -> str:
        # Always empty — nothing to breach.
        return ""


# Module-level singleton; the controller resets it per cycle.
budget = TokenCounter()


def reset_cycle() -> None:
    budget.reset_cycle()


def extract_tokens_from_event(event: dict) -> int:
    """Best-effort extraction of token count from an OpenCode JSON event.

    OpenCode's event schema isn't fully documented, so we look in the common
    spots (usage, tokens, cost fields) across event types. Returns 0 if unknown.
    """
    if not isinstance(event, dict):
        return 0
    for key in ("tokens", "total_tokens"):
        v = event.get(key)
        if isinstance(v, int) and v > 0:
            return v
    usage = event.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            return total
        p = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        c = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        if isinstance(p, int) and isinstance(c, int) and (p or c):
            return p + c
    return 0
