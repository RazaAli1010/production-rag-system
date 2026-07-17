"""F9 (T14): the eval harness's cache posture.

Two properties in tension, and both matter:

1. **Retrieval/RAGAS/refusal can NEVER see a cache hit.** That is the CLAUDE.md rule and the reason
   hit@k is comparable across every label. `--suite all --flags cache=on` must still force it off.
2. **A latency-only run CAN.** F9's gate is latency/cost on a repeat-heavy workload; without this
   the gate is unmeasurable and the feature can never be evaluated at all.
"""

import pytest

from app.core.contracts import PipelineFlags
from app.evals import latency as latency_mod
from app.evals import run as run_mod
from app.evals.flags import parse_flags
from app.evals.schemas import EvalRecord
from tests.evals.conftest import fake_sessionmaker, make_settings

# --------------------------------------------------------------- parse_flags (AC-32/AC-33)

def test_cache_is_forced_off_by_default():
    assert parse_flags("cache=on").cache is False
    assert parse_flags("hybrid=on,rerank=on,cache=on").cache is False


def test_cache_is_honoured_when_explicitly_allowed():
    assert parse_flags("cache=on", allow_cache=True).cache is True
    assert parse_flags("cache=off", allow_cache=True).cache is False


def test_allow_cache_does_not_turn_the_cache_on_by_itself():
    """Opting in must not flip the default — an unspecified flag stays False."""
    assert parse_flags("hybrid=on", allow_cache=True).cache is False
    assert parse_flags(None, allow_cache=True).cache is False


def test_other_flags_are_unaffected_by_allow_cache():
    f = parse_flags("hybrid=on,rerank=on,query_rewrite=on,compression=on", allow_cache=True)
    assert (f.hybrid, f.rerank, f.query_rewrite, f.compression) == (True, True, True, True)


# --------------------------------------------------------------- run.py wiring (AC-33)

@pytest.fixture
def captured_cfg(monkeypatch):
    """Intercepts the EvalConfig `run.main` builds, so we can assert the flags it would run with."""
    seen = {}

    async def _fake_run_suites(cfg, *, settings, sessionmaker):
        seen["cfg"] = cfg
        from app.evals.schemas import EvalRunReport

        return EvalRunReport(label=cfg.label, git_sha="abc123", index_manifest={}, suites=[],
                             flags=cfg.flags)

    async def _fake_write_report(report, settings):
        return "/dev/null"

    monkeypatch.setattr(run_mod, "run_suites", _fake_run_suites)
    monkeypatch.setattr(run_mod, "write_report", _fake_write_report)
    return seen


async def test_suite_all_forces_cache_off_end_to_end(captured_cfg, monkeypatch):
    """The safety property: the headline `--suite all` gate command cannot measure a cache hit
    even if someone passes cache=on."""
    await run_mod.main(
        ["--suite", "all", "--flags", "hybrid=on,cache=on", "--label", "x", "--yes"],
        settings=object(), sessionmaker=object(),
    )
    assert captured_cfg["cfg"].flags.cache is False


async def test_suite_latency_alone_allows_cache_on(captured_cfg):
    """F9's gate command."""
    await run_mod.main(
        ["--suite", "latency", "--flags", "hybrid=on,cache=on", "--label", "f9-cache-after",
         "--yes"],
        settings=object(), sessionmaker=object(),
    )
    assert captured_cfg["cfg"].flags.cache is True


async def test_suite_retrieval_alone_still_forces_cache_off(captured_cfg):
    await run_mod.main(
        ["--suite", "retrieval", "--flags", "cache=on", "--label", "x"],
        settings=object(), sessionmaker=object(),
    )
    assert captured_cfg["cfg"].flags.cache is False


# --------------------------------------------------------------- latency metrics (AC-33b)

def _records():
    return [EvalRecord(qid="q", question="probation rules?", ground_truth_answer="g",
                       source_doc_ids=["d1"], source_pages_or_anchors=["1"], tags=["en"])]


def _astream_factory(cache_hits: list[bool]):
    """An SSE stream whose `meta` reports `cache_hit` per successive call."""
    calls = {"n": 0}

    async def _astream(question, k, namespace, flags, *, session, settings, sessionmaker=None):
        from app.rag.events import SSEEvent, stage_event

        hit = cache_hits[calls["n"] % len(cache_hits)]
        calls["n"] += 1
        yield stage_event("searching", "done", ms=10)
        yield SSEEvent(event="token", data={"token": "answer"})
        yield SSEEvent(event="meta", data={"cache_hit": hit, "tokens_in": 1000, "tokens_out": 50})
        yield SSEEvent(event="done", data={})

    return _astream


async def _run(cache_hits, n=4):
    return await latency_mod.run_latency(
        _records(), PipelineFlags(cache=True), make_settings(EVAL_LATENCY_REQUESTS=n),
        sessionmaker=fake_sessionmaker, astream=_astream_factory(cache_hits),
    )


async def test_cache_hit_rate_and_hit_latency_are_emitted():
    result = await _run([False, True, True, True])
    by_name = {m.metric: m.value for m in result.metrics}

    assert by_name["cache_hit_rate"] == 0.75
    assert "latency_cache_hit_p50" in by_name
    assert "latency_cache_hit_p95" in by_name
    assert by_name["cache_cost_saved_mean"] > 0


async def test_cache_cost_saved_uses_the_central_estimate_cost():
    """3 of 4 requests hit, each avoiding 1000 in / 50 out on gpt-4o-mini."""
    from app.indexing.cost import estimate_cost

    result = await _run([False, True, True, True])
    by_name = {m.metric: m.value for m in result.metrics}

    expected = 3 * estimate_cost("gpt-4o-mini", 1000, 50) / 4
    assert by_name["cache_cost_saved_mean"] == pytest.approx(expected)


async def test_zero_hits_reports_a_zero_rate_not_a_missing_metric():
    result = await _run([False], n=2)
    by_name = {m.metric: m.value for m in result.metrics}

    assert by_name["cache_hit_rate"] == 0.0
    assert by_name["cache_cost_saved_mean"] == 0.0
    assert "latency_cache_hit_p50" not in by_name  # no hits => no hit-latency distribution


async def test_cost_mean_and_tokens_mean_keep_their_pre_f9_basis():
    """THE regression guard for the gate's comparability: `cost_mean` stays output-only and
    `tokens_mean` stays a count of token EVENTS. Changing either would silently turn the
    f9-vs-f8 delta into a comparison of two different measurements (design §9)."""
    from app.indexing.cost import estimate_cost

    result = await _run([False], n=2)
    by_name = {m.metric: m.value for m in result.metrics}

    assert by_name["tokens_mean"] == 1.0  # one `token` event per request, NOT meta's tokens_out=50
    # output-only: input tokens contribute nothing, even though meta now carries tokens_in=1000
    assert by_name["cost_mean"] == estimate_cost("gpt-4o-mini", 0, 1)


# --------------------------------------------------------------- workload shaping

def _many_records(k: int):
    return [EvalRecord(qid=f"q{i}", question=f"question {i}?", ground_truth_answer="g",
                       source_doc_ids=["d1"], source_pages_or_anchors=["1"], tags=["en"])
            for i in range(k)]


async def test_unique_questions_none_samples_every_record():
    """Pre-F9 behaviour, unchanged — every earlier label's workload is unaffected."""
    seen = []

    async def _astream(question, k, ns, flags, *, session, settings, sessionmaker=None):
        from app.rag.events import SSEEvent

        seen.append(question)
        yield SSEEvent(event="meta", data={"cache_hit": False})
        yield SSEEvent(event="done", data={})

    await latency_mod.run_latency(
        _many_records(63), PipelineFlags(),
        make_settings(EVAL_LATENCY_REQUESTS=30, EVAL_LATENCY_UNIQUE_QUESTIONS=None),
        sessionmaker=fake_sessionmaker, astream=_astream,
    )

    # THE bug this setting exists for: 30 requests over 63 records = 30 DISTINCT questions and a
    # 0% hit rate, so the cache would have been measured against a workload with no repeats.
    assert len(seen) == 30
    assert len(set(seen)) == 30


async def test_unique_questions_caps_the_pool_to_create_repeats():
    seen = []

    async def _astream(question, k, ns, flags, *, session, settings, sessionmaker=None):
        from app.rag.events import SSEEvent

        seen.append(question)
        yield SSEEvent(event="meta", data={"cache_hit": False})
        yield SSEEvent(event="done", data={})

    await latency_mod.run_latency(
        _many_records(63), PipelineFlags(),
        make_settings(EVAL_LATENCY_REQUESTS=30, EVAL_LATENCY_UNIQUE_QUESTIONS=15),
        sessionmaker=fake_sessionmaker, astream=_astream,
    )

    assert len(seen) == 30
    assert len(set(seen)) == 15, "30 requests / 15 unique => a DECLARED 50% repeat rate"


def test_metric_names_get_the_right_direction_arrows_from_compare():
    """`compare.py` infers direction from name prefixes, so these exact names are load-bearing."""
    from app.evals.compare import _direction

    assert _direction("cache_hit_rate", 0.5) == "▲"          # more hits is better
    assert _direction("cache_cost_saved_mean", 0.5) == "▲"   # more saved is better
    assert _direction("latency_cache_hit_p95", -100.0) == "▲"  # faster is better
    # the trap this naming avoids:
    assert _direction("cost_saved_mean", 0.5) == "▼", (
        "a 'cost_'-prefixed name would render an improvement as a regression"
    )
