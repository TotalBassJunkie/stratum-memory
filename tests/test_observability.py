"""Tests for the observability layer (:mod:`stratum.observability`, WP-13).

These tests are deliberately **database-free**: :func:`health` is exercised
against a failing database pool (monkeypatched) so the "degraded" path is
deterministic regardless of whether a live Postgres happens to be reachable.
"""

from __future__ import annotations

import json
import logging

import pytest

from stratum.observability import (
    PROMETHEUS_CONTENT_TYPE,
    Metrics,
    default_metrics,
    health,
    log_event,
    metrics_response,
    setup_logging,
)

# --------------------------------------------------------------------------- #
# Metrics: inc / observe / render
# --------------------------------------------------------------------------- #


def test_inc_default_and_accumulate():
    m = Metrics()
    assert m.inc("requests") == 1
    assert m.inc("requests") == 2
    assert m.inc("requests", 5) == 7
    assert m.render_prometheus() == (
        "# TYPE requests counter\nrequests 7\n"
    )


def test_inc_with_labels_renders_sorted_label_set():
    m = Metrics()
    m.inc("requests", labels={"tenant": "t1", "path": "/x"})
    assert m.render_prometheus() == (
        '# TYPE requests counter\nrequests{path="/x",tenant="t1"} 1\n'
    )


def test_inc_label_kwargs_equivalent_to_mapping():
    a = Metrics()
    a.inc("hits", labels={"tenant": "t1"})
    b = Metrics()
    b.inc("hits", tenant="t1")
    assert a.render_prometheus() == b.render_prometheus()


def test_observe_summary_lines():
    m = Metrics()
    m.observe("latency", 0.5)
    m.observe("latency", 1.5)
    m.observe("latency", 1.0)
    assert m.render_prometheus() == (
        "# TYPE latency summary\n"
        "latency_count 3\n"
        "latency_sum 3\n"
        "latency_min 0.5\n"
        "latency_max 1.5\n"
    )


def test_observe_multiple_label_series_sorted():
    m = Metrics()
    m.observe("latency", 2.0, labels={"tenant": "t2"})
    m.observe("latency", 1.0, labels={"tenant": "t1"})
    assert m.render_prometheus() == (
        "# TYPE latency summary\n"
        'latency_count{tenant="t1"} 1\n'
        'latency_sum{tenant="t1"} 1\n'
        'latency_min{tenant="t1"} 1\n'
        'latency_max{tenant="t1"} 1\n'
        'latency_count{tenant="t2"} 1\n'
        'latency_sum{tenant="t2"} 2\n'
        'latency_min{tenant="t2"} 2\n'
        'latency_max{tenant="t2"} 2\n'
    )


def test_set_gauge_renders_last_value():
    m = Metrics()
    m.set_gauge("queue_depth", 3)
    m.set_gauge("queue_depth", 1)
    assert m.render_prometheus() == "# TYPE queue_depth gauge\nqueue_depth 1\n"


def test_render_is_deterministic_regardless_of_insertion_order():
    a = Metrics()
    a.inc("zebra")
    a.inc("alpha")
    a.set_gauge("mid", 4)

    b = Metrics()
    b.set_gauge("mid", 4)
    b.inc("alpha")
    b.inc("zebra")

    assert a.render_prometheus() == b.render_prometheus()


def test_render_type_line_precedes_series():
    m = Metrics()
    m.inc("c", 2)
    m.set_gauge("g", 7)
    m.observe("o", 9)
    body = m.render_prometheus()
    assert "# TYPE c counter" in body
    assert "# TYPE g gauge" in body
    assert "# TYPE o summary" in body
    # families are alphabetically ordered
    assert body.index("# TYPE c counter") < body.index("# TYPE g gauge") < body.index(
        "# TYPE o summary"
    )
    assert body.endswith("\n")


def test_inc_and_set_gauge_kind_conflict_raises():
    m = Metrics()
    m.inc("x")
    with pytest.raises(ValueError):
        m.set_gauge("x", 1)


# --------------------------------------------------------------------------- #
# Tokens / cost
# --------------------------------------------------------------------------- #


def test_add_tokens_cost_summary_per_tenant_and_model():
    m = Metrics()
    m.add_tokens("t1", 1000, 500, model="gpt-4o-mini")
    m.add_tokens("t1", 1000, 500, model="gpt-4o-mini")  # accumulates
    m.add_tokens("t2", 200, 100, model="gpt-4o-mini")
    m.add_tokens("t1", 10, 20)  # DEFAULT_MODEL

    summary = m.cost_summary()
    assert set(summary) == {"t1", "t2"}

    # gpt-4o-mini: 0.00015 / 1k prompt, 0.0006 / 1k completion
    t1_mini = summary["t1"]["gpt-4o-mini"]
    assert t1_mini["prompt"] == 2000
    assert t1_mini["completion"] == 1000
    assert t1_mini["total"] == 3000
    expected = 2000 / 1000 * 0.00015 + 1000 / 1000 * 0.0006
    assert t1_mini["cost_usd"] == pytest.approx(expected)

    assert summary["t1"]["unknown"]["total"] == 30
    assert summary["t1"]["unknown"]["cost_usd"] == 0.0

    assert summary["t2"]["gpt-4o-mini"]["total"] == 300


def test_cost_summary_unknown_model_is_free():
    m = Metrics()
    m.add_tokens("t1", 5000, 5000, model="no-such-model")
    entry = m.cost_summary()["t1"]["no-such-model"]
    assert entry["cost_usd"] == 0.0
    assert entry["total"] == 10000


def test_cost_summary_empty():
    assert Metrics().cost_summary() == {}


# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #


@pytest.fixture
def _restore_root_logger():
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    root.handlers[:] = saved


def test_log_event_emits_parseable_json(capsys, _restore_root_logger):
    setup_logging("INFO")
    log_event(logging.getLogger("stratum.test"), "did a thing", tenant="t1", count=3)

    err = capsys.readouterr().err.strip()
    payload = json.loads(err)
    assert payload["msg"] == "did a thing"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "stratum.test"
    assert payload["tenant"] == "t1"
    assert payload["count"] == 3
    assert "time" in payload and "T" in payload["time"]


def test_log_event_accepts_level_name(capsys, _restore_root_logger):
    setup_logging("DEBUG")
    log_event("stratum.named", "warned", level="WARNING", code=42)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["level"] == "WARNING"
    assert payload["code"] == 42


def test_log_event_reserved_field_is_renamed(capsys, _restore_root_logger):
    setup_logging("INFO")
    # ``name`` collides with LogRecord.name and must not blow up logging.
    log_event("stratum.test", "ok", name="collision")
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["field_name"] == "collision"
    assert payload["msg"] == "ok"


def test_json_formatter_includes_exception(capsys, _restore_root_logger):
    setup_logging("INFO")
    logger = logging.getLogger("stratum.err")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    err = capsys.readouterr().err.strip()
    payload = json.loads(err)
    assert payload["level"] == "ERROR"
    assert "ValueError: boom" in payload["exc_info"]


def test_setup_logging_does_not_duplicate_handlers(_restore_root_logger):
    setup_logging("INFO")
    setup_logging("INFO")
    assert len(logging.getLogger().handlers) == 1


# --------------------------------------------------------------------------- #
# health / metrics_response
# --------------------------------------------------------------------------- #


def test_health_without_db_is_degraded_and_never_raises(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise ConnectionError("no database")

    monkeypatch.setattr("stratum.db.get_pool", _boom)

    result = health()
    assert result["status"] == "degraded"
    assert result["db"]["ok"] is False
    assert "no database" in result["db"]["error"]
    assert result["jobs"]["queued"] is None
    assert result["version"]


def test_health_ok_when_db_reachable(monkeypatch):
    monkeypatch.setattr(
        "stratum.observability._check_db", lambda timeout: (True, None)
    )
    monkeypatch.setattr(
        "stratum.observability._check_queued_jobs", lambda: (7, None)
    )
    result = health()
    assert result["status"] == "ok"
    assert result["db"]["ok"] is True
    assert result["jobs"]["queued"] == 7
    assert result["git"]["ok"] in (True, False)
    assert result["llm"]["ok"] in (True, False)


def test_health_llm_flag_reflects_settings(monkeypatch):
    from stratum.config import Settings

    monkeypatch.setattr(
        "stratum.observability._check_db", lambda timeout: (False, "down")
    )
    monkeypatch.setattr(
        "stratum.config.get_settings", lambda: Settings(llm_base_url="http://x")
    )
    assert health()["llm"]["ok"] is True


def test_metrics_response_default_registry():
    body, content_type = metrics_response()
    assert content_type == PROMETHEUS_CONTENT_TYPE
    assert isinstance(body, str)


def test_metrics_response_explicit_registry():
    m = Metrics()
    m.inc("custom_metric", 3)
    body, content_type = metrics_response(m)
    assert "custom_metric 3" in body
    assert content_type == PROMETHEUS_CONTENT_TYPE


def test_default_metrics_is_singleton():
    assert default_metrics() is default_metrics()


def test_check_queued_jobs_uses_bypass_rls(monkeypatch):
    """B12: the queue probe must read across tenants via ``bypass_rls=True``."""
    import stratum.db as db_mod
    import stratum.observability as obs

    seen: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            seen["sql"] = sql

        def fetchone(self):
            return {"n": 7}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cursor()

    class _CM:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(db_mod, "connection", lambda **kw: _CM(**kw))
    count, error = obs._check_queued_jobs()
    assert count == 7
    assert error is None
    assert seen["kwargs"] == {"bypass_rls": True}
