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

    Handles both documented and observed schema shapes:
      - {"usage": {"total_tokens": N}}            (OpenAI-style)
      - {"tokens": {"total": N, "input": N, ...}} (OpenCode step_finish part.tokens)
      - {"total_tokens": N} or {"tokens": N}      (plain)
    Returns 0 if unknown.
    """
    if not isinstance(event, dict):
        return 0
    # direct int fields
    for key in ("total_tokens", "tokens"):
        v = event.get(key)
        if isinstance(v, int) and v > 0:
            return v
    # nested usage/tokens dict — accept both total_tokens and total keys
    for nest_key in ("usage", "tokens"):
        nest = event.get(nest_key)
        if isinstance(nest, dict):
            total = nest.get("total_tokens")
            if isinstance(total, int) and total > 0:
                return total
            total = nest.get("total")
            if isinstance(total, int) and total > 0:
                return total
            # sum input + output if total absent
            inp = nest.get("input_tokens") or nest.get("input") or 0
            outp = nest.get("output_tokens") or nest.get("output") or 0
            if isinstance(inp, int) and isinstance(outp, int) and (inp or outp):
                return inp + outp
    return 0
