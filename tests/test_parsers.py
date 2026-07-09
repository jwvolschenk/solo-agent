"""Parser tests — both classic (b2700) and current (master) llama-server schemas."""

from src.parsers import (
    live_context_tokens,
    parse_health,
    parse_prometheus,
    parse_props,
    parse_slots,
    slot_context_tokens,
)


# -- Prometheus ---------------------------------------------------------------


def test_prometheus_parses_all_known_metrics(prometheus_text):
    snap = parse_prometheus(prometheus_text)
    assert snap.prompt_tokens_seconds == 2450.0
    assert snap.predicted_tokens_seconds == 94.0
    assert snap.prompt_tokens_total == 1234
    assert snap.tokens_predicted_total == 789
    assert snap.requests_processing == 1
    assert snap.requests_deferred == 0
    assert snap.n_tokens_max == 5000
    assert snap.kv_cache_tokens == 525
    assert snap.kv_cache_usage_ratio == 0.002
    assert snap.n_decode_total == 100


def test_prometheus_collects_unknown_metrics_into_raw(prometheus_text):
    snap = parse_prometheus(prometheus_text)
    # every llamacpp: line is captured even if not projected to a field
    assert "prompt_tokens_total" in snap.raw
    assert "n_busy_slots_per_decode" in snap.raw
    assert snap.raw["n_busy_slots_per_decode"] == 1.0


def test_prometheus_tolerates_empty_and_garbage():
    snap = parse_prometheus("")
    assert snap.prompt_tokens_seconds is None
    assert snap.raw == {}
    # garbage lines don't crash
    snap2 = parse_prometheus("not a metric\n# comment\nllamacpp:foo bar\n")
    assert "foo" not in snap2.raw  # 'bar' isn't a float


def test_prometheus_skips_non_llamacpp_lines():
    text = "python_gc_objects_collected 42\nllamacpp:tokens_predicted_total 10\n"
    snap = parse_prometheus(text)
    assert snap.tokens_predicted_total == 10
    assert "python_gc_objects_collected" not in snap.raw


# -- Slots --------------------------------------------------------------------


def test_slots_current_schema(slots_current):
    slots = parse_slots(slots_current)
    assert len(slots) == 2
    assert slots[0].is_processing is False
    assert slots[1].is_processing is True
    assert slots[1].n_decoded == 25  # pulled from next_token
    assert slots[1].n_prompt_tokens == 500
    assert slots[1].n_prompt_tokens_processed == 500
    assert slots[1].generated_text == "hello world"
    assert slots[0].n_ctx == 262144


def test_slot_context_tokens_active_slot(slots_current):
    slots = parse_slots(slots_current)
    assert slot_context_tokens(slots[1]) == 525  # 500 prompt + 25 decoded


def test_live_context_tokens_prefers_processing_slot(slots_current):
    slots = parse_slots(slots_current)
    snap = parse_prometheus(
        "llamacpp:kv_cache_tokens 9999\nllamacpp:kv_cache_usage_ratio 0.5\n"
    )
    assert live_context_tokens(slots, snap) == 525


def test_live_context_tokens_idle_uses_cache_fields():
    slots = parse_slots([
        {"id": 0, "n_ctx": 4096, "is_processing": False,
         "n_prompt_tokens_cache": 1200, "next_token": {"n_decoded": 0}},
    ])
    assert live_context_tokens(slots) == 1200


def test_live_context_tokens_falls_back_to_kv_cache_gauge():
    slots = parse_slots([{"id": 0, "n_ctx": 4096, "is_processing": False}])
    snap = parse_prometheus("llamacpp:kv_cache_tokens 800\n")
    assert live_context_tokens(slots, snap) == 800


def test_live_context_tokens_zero_when_idle_and_empty():
    slots = parse_slots([{"id": 0, "n_ctx": 4096, "is_processing": False}])
    assert live_context_tokens(slots) == 0


def test_slots_classic_schema(slots_classic):
    """Classic uses numeric 'state' instead of is_processing bool."""
    slots = parse_slots(slots_classic)
    assert len(slots) == 2
    # state 0 -> idle, state 1 -> processing
    assert slots[0].is_processing is False
    assert slots[1].is_processing is True
    # n_decoded is nested under next_token in both schemas
    assert slots[1].n_decoded == 100
    assert slots[1].model == "qwen-test"  # classic-only field


def test_slots_empty_and_malformed():
    assert parse_slots([]) == []
    assert parse_slots({}) == []
    assert parse_slots("not a list") == []
    assert parse_slots([{"id": 0}])[0].is_processing is False


# -- Health -------------------------------------------------------------------


def test_health_current_ok():
    h = parse_health(200, {"status": "ok"})
    assert h.status == "ok"
    assert h.http_status == 200


def test_health_classic_ok_has_slot_counts():
    h = parse_health(200, {"status": "ok", "slots_idle": 2, "slots_processing": 0})
    assert h.status == "ok"
    assert h.slots_idle == 2
    assert h.slots_processing == 0


def test_health_offline_when_unreachable():
    h = parse_health(0, None)
    assert h.status == "offline"
    assert h.http_status == 0


def test_health_error_body_loading():
    h = parse_health(503, {"error": {"message": "Loading model"}})
    assert h.status == "loading"
    assert "Loading" in h.message


def test_health_error_body_generic():
    h = parse_health(503, {"error": {"message": "Model failed to load"}})
    assert h.status == "error"


def test_health_no_slot_available():
    h = parse_health(200, {"status": "no slot available", "slots_idle": 0})
    assert h.status == "no_slot_available"


def test_health_non_json_body_falls_back_to_http_status():
    h = parse_health(200, "<html>ok</html>")
    assert h.status == "ok"  # 200 -> ok even with non-JSON body


# -- Props --------------------------------------------------------------------


def test_props_current_nested_params(props_current):
    p = parse_props(props_current)
    assert p.model_alias == "qwen-test"
    assert p.total_slots == 1
    assert p.n_ctx == 262144
    # params deep-got from default_generation_settings.params
    assert p.params["temperature"] == 0.8
    assert p.params["top_k"] == 20


def test_props_classic_flat_params(props_classic):
    p = parse_props(props_classic)
    assert p.model_alias == "qwen-classic"
    assert p.total_slots == 2
    # classic: params are flat in default_generation_settings
    assert p.params["temperature"] == 0.7
    assert p.params["top_k"] == 40


def test_props_missing_fields_dont_crash():
    p = parse_props({})
    assert p.model_alias is None
    assert p.params == {}
