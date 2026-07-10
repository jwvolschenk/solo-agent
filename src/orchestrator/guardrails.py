"""Guardrails — circuit breakers for 24/7 autonomous operation.

Each guardrail is a small, independent detector. The controller consults them
between phases. If any trips, the controller pauses + surfaces the reason.

Detectors:
  - LoopDetector: identical action sequences repeated -> doom_loop equivalent
  - NoProgressDetector: cycles producing < threshold change
  - DiminishingReturns: N consecutive verify-fails
  - KillSwitch: external stop signal (honored even mid-cycle)
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from . import trace

log = logging.getLogger("solo.guardrails")


@dataclass
class LoopDetector:
    """Detects repeated identical tool-call sequences.

    Hashes a sliding window of recent actions; if the same hash repeats too
    often within a goal, the agent is stuck in a loop.
    """

    window: int = 6
    repeat_threshold: int = 3
    _hashes: deque[str] = field(default_factory=lambda: deque(maxlen=200))

    def observe(self, action_desc: str) -> bool:
        """Record an action; return True if a loop is detected."""
        h = hashlib.sha1(action_desc.encode("utf-8")).hexdigest()[:12]
        self._hashes.append(h)
        if len(self._hashes) < self.window * self.repeat_threshold:
            return False
        recent = list(self._hashes)[-self.window:]
        # if the last `window` actions equal the previous `window` actions, it's a loop
        if len(self._hashes) >= self.window * 2:
            prev = list(self._hashes)[-self.window * 2:-self.window]
            if recent == prev:
                log.warning("LOOP DETECTED: action sequence repeating")
                trace.guardrail("loop", "action sequence repeating")
                return True
        return False

    def reset(self) -> None:
        self._hashes.clear()


@dataclass
class NoProgressDetector:
    """Flags when the last N cycles changed fewer than `min_lines` lines."""

    history: deque[int] = field(default_factory=lambda: deque(maxlen=10))

    def observe(self, lines_changed: int) -> bool:
        self.history.append(lines_changed)
        if len(self.history) < settings.stall_detection_cycles:
            return False
        recent = list(self.history)[-settings.stall_detection_cycles:]
        if all(n < settings.stall_min_lines_changed for n in recent):
            log.warning(
                "NO PROGRESS: %d cycles < %d lines changed",
                settings.stall_detection_cycles,
                settings.stall_min_lines_changed,
            )
            trace.guardrail(
                "no_progress",
                f"{settings.stall_detection_cycles} cycles below line threshold",
                min_lines=settings.stall_min_lines_changed,
            )
            return True
        return False

    def reset(self) -> None:
        self.history.clear()


@dataclass
class KillSwitch:
    """External stop signal. Set via API/dashboard/signal; honored ASAP."""

    _stop: bool = False
    reason: str = ""

    def request_stop(self, reason: str = "manual") -> None:
        self._stop = True
        self.reason = reason
        log.warning("KILL SWITCH ENGAGED: %s", reason)
        trace.guardrail("kill_switch", reason)

    @property
    def engaged(self) -> bool:
        return self._stop

    def clear(self) -> None:
        self._stop = False
        self.reason = ""


# Module-level singletons the controller uses.
loop_detector = LoopDetector()
no_progress = NoProgressDetector()
kill_switch = KillSwitch()


def reset_all() -> None:
    """Reset per-cycle/per-goal detectors (call at the start of each goal)."""
    loop_detector.reset()
