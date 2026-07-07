"""Defensive parsers for llama-server responses.

llama.cpp's endpoint schemas changed between versions. The TurboQuant fork's
exact version isn't pinned, so every parser here tolerates BOTH:
  - current (master / b9902+): {"status":"ok"} /health, is_processing bool, etc.
  - classic (b2700~):          {"status":"ok","slots_idle":N,...}, numeric state, etc.

If a field is absent it stays None — never raises. The monitoring layer marks
the server "offline" only when the HTTP call itself fails.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .models import HealthState, MetricsSnapshot, Props, SlotInfo


# ----------------------------------------------------------------------------
# /metrics — Prometheus text format, llamacpp: prefix
# ----------------------------------------------------------------------------


def parse_prometheus(text: str) -> MetricsSnapshot:
    """Parse Prometheus exposition text into a MetricsSnapshot.

    We collect EVERY ``llamacpp:<name> <value>`` line into ``raw`` (so version
    drift in metric lists is harmless), then project the well-known fields.
    """
    raw: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("llamacpp:"):
            continue
        # "llamacpp:name value" or "llamacpp:name{labels} value"
        rest = line[len("llamacpp:") :]
        # strip optional {labels}
        if "{" in rest:
            rest = rest[rest.index("}") + 1 :]
        parts = rest.split()
        if len(parts) < 2:
            continue
        name, value_str = parts[0], parts[1]
        try:
            value: float = float(value_str)
        except ValueError:
            continue
        raw[name] = value

    def _get_int(key: str) -> Optional[int]:
        v = raw.get(key)
        return int(v) if v is not None else None

    def _get_float(key: str) -> Optional[float]:
        v = raw.get(key)
        return float(v) if v is not None else None

    return MetricsSnapshot(
        prompt_tokens_seconds=_get_float("prompt_tokens_seconds"),
        predicted_tokens_seconds=_get_float("predicted_tokens_seconds"),
        prompt_tokens_total=_get_int("prompt_tokens_total"),
        tokens_predicted_total=_get_int("tokens_predicted_total"),
        prompt_seconds_total=_get_float("prompt_seconds_total"),
        tokens_predicted_seconds_total=_get_float("tokens_predicted_seconds_total"),
        n_decode_total=_get_int("n_decode_total"),
        n_tokens_max=_get_int("n_tokens_max"),
        requests_processing=_get_int("requests_processing"),
        requests_deferred=_get_int("requests_deferred"),
        n_busy_slots_per_decode=_get_float("n_busy_slots_per_decode"),
        raw=raw,
    )


# ----------------------------------------------------------------------------
# /slots — JSON array of slot objects (both versions)
# ----------------------------------------------------------------------------


def parse_slots(data: Any) -> list[SlotInfo]:
    """Parse the /slots response (a JSON array) into SlotInfo objects.

    Defensive: handles ``is_processing`` (current) vs numeric ``state`` (classic),
    and pulls ``n_decoded`` from the nested ``next_token`` object if present.
    """
    if isinstance(data, dict) and "slots" in data:
        data = data["slots"]
    if not isinstance(data, list):
        return []

    out: list[SlotInfo] = []
    for raw_slot in data:
        if not isinstance(raw_slot, dict):
            continue
        sid = raw_slot.get("id", 0)
        # is_processing: current has a bool; classic has numeric 'state' (0=idle)
        is_processing: bool
        if "is_processing" in raw_slot:
            is_processing = bool(raw_slot["is_processing"])
        elif "state" in raw_slot:
            is_processing = int(raw_slot["state"]) != 0
        else:
            is_processing = False

        next_token = raw_slot.get("next_token") or {}
        n_decoded = next_token.get("n_decoded") if isinstance(next_token, dict) else None
        # current schema: 'generated'; some docs/notes say 'generated_text' — accept both
        generated = raw_slot.get("generated", raw_slot.get("generated_text"))

        out.append(
            SlotInfo(
                id=int(sid),
                n_ctx=int(raw_slot.get("n_ctx", 0) or 0),
                is_processing=is_processing,
                n_prompt_tokens=_opt_int(raw_slot.get("n_prompt_tokens")),
                n_decoded=_opt_int(n_decoded),
                generated_text=generated if isinstance(generated, str) else None,
                model=raw_slot.get("model") if isinstance(raw_slot.get("model"), str) else None,
                raw=raw_slot,
            )
        )
    return out


# ----------------------------------------------------------------------------
# /health — two shapes across versions
# ----------------------------------------------------------------------------


def parse_health(http_status: int, body: Any) -> HealthState:
    """Parse /health. http_status: 200=ok, 503=loading/error, 0=unreachable.

    Body shapes:
      current:  {"status":"ok"}
      classic:  {"status":"ok","slots_idle":N,"slots_processing":N}
      failure:  {"error":{"message":"Loading model",...}}  (both versions)
    """
    if http_status == 0:
        return HealthState(status="offline", http_status=0, message="unreachable")

    # Try to decode JSON; if it fails, fall back to http_status mapping.
    payload: dict[str, Any] = {}
    if isinstance(body, (dict, list)):
        payload = body if isinstance(body, dict) else {"_": body}
    elif isinstance(body, str) and body.strip():
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}

    # Error body present in both versions for loading/error states.
    if isinstance(payload.get("error"), dict):
        err = payload["error"]
        msg = str(err.get("message", "error"))
        # loading vs error: heuristic on the message
        lowered = msg.lower()
        if "loading" in lowered:
            return HealthState(status="loading", http_status=http_status, message=msg)
        return HealthState(status="error", http_status=http_status, message=msg)

    status_str = str(payload.get("status", "")).lower()
    slots_idle = _opt_int(payload.get("slots_idle"))
    slots_processing = _opt_int(payload.get("slots_processing"))

    if http_status == 200:
        if status_str == "no slot available":
            return HealthState(
                status="no_slot_available",
                http_status=http_status,
                message=status_str or "no slot available",
                slots_idle=slots_idle,
                slots_processing=slots_processing,
            )
        # default healthy
        return HealthState(
            status="ok",
            http_status=http_status,
            message=status_str or "ok",
            slots_idle=slots_idle,
            slots_processing=slots_processing,
        )

    # Non-200, non-error-body: treat as error.
    return HealthState(
        status="error",
        http_status=http_status,
        message=str(payload.get("status") or f"HTTP {http_status}"),
    )


# ----------------------------------------------------------------------------
# /props — model info + generation params
# ----------------------------------------------------------------------------


def parse_props(data: Any) -> Props:
    """Parse /props. Sampling params nested under default_generation_settings.params
    (current) or flat in default_generation_settings (classic). We deep-get to a flat dict.
    """
    if not isinstance(data, dict):
        return Props(raw=data if isinstance(data, dict) else {})

    dgs = data.get("default_generation_settings") or {}

    # current: dgs = {"params": {...}, "n_ctx": N}; classic: dgs = {flat params...}
    if isinstance(dgs.get("params"), dict):
        params = dict(dgs["params"])
    else:
        params = dict(dgs) if isinstance(dgs, dict) else {}

    n_ctx = dgs.get("n_ctx") if isinstance(dgs, dict) else None

    return Props(
        model_alias=data.get("model_alias") or data.get("model"),
        model_path=data.get("model_path"),
        total_slots=_opt_int(data.get("total_slots")),
        chat_template=data.get("chat_template"),
        bos_token=data.get("bos_token"),
        eos_token=data.get("eos_token"),
        n_ctx=_opt_int(n_ctx) if n_ctx is not None else None,
        params=params,
        raw=data,
    )


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _opt_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
