"""F13 — observability: request_logs write path, privacy, stats, graceful Langfuse.

Reuses the F11 API conftest (`client`/`admin`/`session`/`make_fake_astream`) — the fake pipeline
means zero OpenAI/Pinecone calls. The write-behind row is drained explicitly before asserting.
"""

import datetime as dt
import uuid

from sqlalchemy import delete, func, select

from app.api import ask as ask_router
from app.caching.keys import exact_key, normalize
from app.db.enums import RequestChannel
from app.db.models import CacheEntry, RequestLog
from app.db.models.chat import Message, MessageRole, Session
from app.observability import request_log
from app.observability.stats import gather_stats
from tests.api.conftest import Recorder, make_fake_astream


async def _count_logs(session) -> int:
    return (await session.execute(select(func.count()).select_from(RequestLog))).scalar_one()


# --------------------------------------------------------------------------- write path + correlation

async def test_ask_writes_one_correlated_request_log(client, session, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await client.post("/api/ask", json={"question": "what is the probation rule"},
                          headers={"Accept": "application/json"})
    assert r.status_code == 200
    await request_log.drain_writes()

    rows = (await session.execute(select(RequestLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    # one request_id resolves to the response header AND the row (AC-3/9).
    assert row.request_id == r.headers["x-request-id"] == r.json()["request_id"]
    assert row.http_status == 200
    assert row.channel == RequestChannel.web
    assert row.total_ms is not None
    # hash of the question, never the text (AC-5).
    assert row.query_hash == exact_key(normalize("what is the probation rule"))


async def test_write_is_behind_not_on_response_path(client, session, monkeypatch):
    """The POST returns before the row is committed; it appears only after the task drains (AC-4)."""
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await client.post("/api/ask", json={"question": "a valid question here"},
                          headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert request_log._WRITE_TASKS, "a write-behind task should be in flight, not awaited inline"
    await request_log.drain_writes()
    assert await _count_logs(session) == 1


async def test_health_writes_no_request_log(client, session):
    """request_logs is ask-only — a health probe must not create a row (AC-6)."""
    await client.get("/api/health")
    await request_log.drain_writes()
    assert await _count_logs(session) == 0


# --------------------------------------------------------------------------- privacy (AC-14)

async def test_raw_query_never_lands_in_request_logs(client, session, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    secret = "zzq unique probation phrase 4271"
    await client.post("/api/ask", json={"question": secret},
                      headers={"Accept": "application/json"})
    await request_log.drain_writes()

    row = (await session.execute(select(RequestLog))).scalars().one()
    # the hash is present; the raw text is in no column.
    assert row.query_hash == exact_key(normalize(secret))
    for value in (row.request_id, row.query_hash, row.model, row.error_type,
                  str(row.channel.value)):
        assert secret not in (value or "")


# --------------------------------------------------------------------------- stats (AC-10)

async def _seed_log(session, **o):
    row = {
        "request_id": uuid.uuid4().hex, "channel": RequestChannel.web,
        "query_hash": uuid.uuid4().hex, "pipeline_flags": {"hybrid": True, "rerank": False},
        "cache_hit": False, "refused": False, "degraded": False, "memory_summarized": False,
        "tokens_in": 100, "tokens_out": 50, "est_cost_usd": 0.001,
        "model": "gpt-4o-mini", "http_status": 200, "total_ms": 200,
    }
    row.update(o)
    session.add(RequestLog(**row))


async def test_stats_aggregates(session):
    # cache_entries is cumulative (not window-scoped) and not in the autouse truncate, so clear it
    # for a deterministic tokens_saved_by_cache / top_clusters assertion.
    await session.execute(delete(CacheEntry))
    # 4 requests: 1 cache hit, 1 refusal, 1 error(500), 1 summarized; known latencies for p50/p95.
    await _seed_log(session, total_ms=100, cache_hit=True, est_cost_usd=0.0)
    await _seed_log(session, total_ms=200, refused=True)
    await _seed_log(session, total_ms=300, http_status=500, error_type="provider_error")
    await _seed_log(session, total_ms=400, memory_summarized=True, degraded=True)
    session.add(CacheEntry(query_hash=uuid.uuid4().hex, query_text="fee refund policy",
                           embedding=b"\x00" * 4, answer={"tokens_out": 40},
                           index_manifest_id="m1", hits=5))
    s = Session(id=uuid.uuid4(), last_active_at=func.now())
    session.add(s)
    await session.flush()
    session.add(Message(session_id=s.id, role=MessageRole.user, content="q", token_count=1))
    session.add(Message(session_id=s.id, role=MessageRole.assistant, content="a", token_count=1))
    await session.commit()

    stats = await gather_stats(session, dt.timedelta(hours=24), summary_budget=600)

    assert stats.request_count == 4
    assert stats.cache_hit_rate == 0.25
    assert stats.refusal_rate == 0.25
    assert stats.error_rate == 0.25
    assert stats.degraded_rate == 0.25
    assert stats.p50_ms is not None and 200 <= stats.p50_ms <= 300
    assert stats.flag_usage.get("hybrid") == 4 and stats.flag_usage.get("rerank", 0) == 0
    assert stats.tokens_saved_by_cache == 5 * 40
    assert stats.top_query_clusters and stats.top_query_clusters[0]["query"] == "fee refund policy"
    assert stats.active_sessions == 1
    assert stats.mean_turns_per_session == 2.0
    assert stats.summarization_count == 1
    assert stats.tokens_saved_by_summarization_est == 600


async def test_stats_endpoint_requires_admin(client, admin):
    assert (await client.get("/internal/stats")).status_code in (401, 403)
    r = await client.get("/internal/stats", headers=admin["headers"])
    assert r.status_code == 200
    assert "request_count" in r.json()
    assert (await client.get("/internal/stats?window=bad",
                             headers=admin["headers"])).status_code == 422


# --------------------------------------------------------------------------- graceful Langfuse (AC-2)

def test_langfuse_handler_none_when_unconfigured(api_settings):
    from app.rag.observability import langfuse_handler

    assert langfuse_handler(session_id=None, settings=api_settings) is None
