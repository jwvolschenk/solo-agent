"""Build a bounded memory brief injected into each agent prompt.

This is deterministic (no extra model call): parse reflections, rank by signal,
truncate aggressively, and emit a small markdown block.
"""

from __future__ import annotations

from typing import Iterable

from ..config import settings
from ..state_reader import read_reflections


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _score(outcome: str, text: str) -> int:
    t = text.lower()
    score = 0
    if outcome in ("failed", "errored"):
        score += 100
    elif outcome == "pending":
        score += 70
    elif outcome == "passed":
        score += 20

    if "reflect phase:" in t:
        score += 60
    if "verify fail" in t or "revert" in t or "error" in t or "blocked" in t:
        score += 50
    if "executed " in t and " completed" in t:
        score -= 30
    return score


def _iter_candidates() -> Iterable[tuple[int, int, str, str]]:
    sf = read_reflections()
    # Newest first so ties prefer recency.
    for idx, r in enumerate(reversed(sf.reflections)):
        text = _normalize(r.text)
        if not text:
            continue
        label = f"cycle {r.cycle} [{r.outcome}]"
        yield (_score(r.outcome, text), -idx, label, text)


def build_memory_brief() -> str:
    """Return a bounded markdown snippet for prompt injection."""
    if settings.memory_brief_max_chars <= 0 or settings.memory_brief_max_items <= 0:
        return ""

    ranked = sorted(_iter_candidates(), key=lambda x: (x[0], x[1]), reverse=True)
    if not ranked:
        return ""

    lines: list[str] = []
    for _, _, label, text in ranked[: settings.memory_brief_max_items]:
        item = f"- {label}: {_cap(text, settings.memory_brief_item_max_chars)}"
        lines.append(item)

    block = "RECENT MEMORY (curated by orchestrator):\n" + "\n".join(lines)
    return _cap(block, settings.memory_brief_max_chars)
