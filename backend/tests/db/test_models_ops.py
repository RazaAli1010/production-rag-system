"""T-8: RequestLog (all stage timings) and CacheEntry (BYTEA embedding round-trip)."""

import struct
import uuid

import pytest

from app.db.enums import RequestChannel
from app.db.models import CacheEntry, RequestLog


@pytest.mark.asyncio
async def test_request_log_all_stage_timings(session):
    log = RequestLog(
        request_id=f"req-{uuid.uuid4().hex}",
        user_id=None,
        session_id=None,
        channel=RequestChannel.web,
        query_hash="deadbeef",
        pipeline_flags={"hybrid": True, "rerank": False},
        cache_hit=False,
        refused=False,
        degraded=False,
        memory_summarized=False,
        embed_ms=10,
        retrieve_ms=20,
        rerank_ms=30,
        rewrite_ms=5,
        memory_ms=1,
        summarize_ms=0,
        llm_ms=400,
        total_ms=466,
        tokens_in=100,
        tokens_out=50,
        est_cost_usd=0.0012,
        model="gpt-4o-mini",
        http_status=200,
        error_type=None,
    )
    session.add(log)
    await session.flush()

    fetched = await session.get(RequestLog, log.request_id)
    assert fetched.pipeline_flags == {"hybrid": True, "rerank": False}
    assert fetched.total_ms == 466
    assert fetched.embed_ms == 10 and fetched.llm_ms == 400


@pytest.mark.asyncio
async def test_cache_entry_embedding_round_trip(session):
    vector = [0.1 * i for i in range(1536)]
    raw = struct.pack(f"<{len(vector)}f", *vector)  # float32[1536] ~= 6 KB
    assert len(raw) == 1536 * 4

    entry = CacheEntry(
        query_text="probation se kaise nikalta hoon",
        embedding=raw,
        answer={"answer": "...", "citations": []},
        index_manifest_id="manifest-1",
        hits=0,
    )
    session.add(entry)
    await session.flush()

    fetched = await session.get(CacheEntry, entry.id)
    assert fetched.embedding == raw  # byte-identical
