"""Budget governor — per-cycle and per-day token ceilings.

Parses token usage from OpenCode's ``--format json`` event stream and tracks it
against the configured budgets. Breaching either pauses the loop and surfaces
an alert to the human.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ..config import settings
from ..db import add_tokens, tokens_for_day

log = logging.getLogger("solo.budget")


@dataclass
class BudgetState:
    cycle_tokens: int = 0
    day: str = ""
    day_tokens: int = 0
    cycle_limit: int = settings.cycle_token_budget
    day_limit: int = settings.daily_token_budget
    breached: str = ""  # "" | "cycle" | "day"

    def reset_cycle(self) -> None:
        self.cycle_tokens = 0
        self.breached = ""

    def rollover_day_if_needed(self) -> None:
        today = date.today().isoformat()
        if self.day != today:
            self.day = today
            self.day_tokens = tokens_for_day(today)

    def add(self, tokens: int) -> None:
        """Record tokens used this cycle + today. Sets breached if a limit is hit."""
        if tokens <= 0:
            return
        self.rollover_day_if_needed()
        self.cycle_tokens += tokens
        self.day_tokens = add_tokens(self.day, tokens)
        if self.cycle_tokens >= self.cycle_limit and not self.breached:
            self.breached = "cycle"
            log.warning(
                "CYCLE BUDGET BREACHED: %d >= %d tokens", self.cycle_tokens, self.cycle_limit
            )
        elif self.day_tokens >= self.day_limit and not self.breached:
            self.breached = "day"
            log.warning(
                "DAILY BUDGET BREACHED: %d >= %d tokens", self.day_tokens, self.day_limit
            )

    @property
    def ok(self) -> bool:
        return not self.breached


# Module-level singleton; the controller resets it per cycle.
budget = BudgetState()


def reset_cycle() -> None:
    budget.reset_cycle()


def extract_tokens_from_event(event: dict) -> int:
    """Best-effort extraction of token count from an OpenCode JSON event.

    OpenCode's event schema isn't fully documented, so we look in the common
    spots (usage, tokens, cost fields) across event types. Returns 0 if unknown.
    """
    if not isinstance(event, dict):
        return 0
    # try a few known shapes
    for key in ("tokens", "total_tokens"):
        v = event.get(key)
        if isinstance(v, int) and v > 0:
            return v
    usage = event.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            return total
        # sum prompt + completion if present
        p = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        c = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        if isinstance(p, int) and isinstance(c, int) and (p or c):
            return p + c
    return 0
