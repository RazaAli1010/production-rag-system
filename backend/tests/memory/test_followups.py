"""T13 / AC-30 — the 10-dialogue follow-up quality signal (separate from the F4 suites).

F17's job is to DELIVER the prior turn's memory to the pipeline so F7's rewrite can condense a bare
follow-up into a standalone question. The actual condensation is F7-tested; here we assert the
contract F17 owns: turn 2's pipeline receives a MemoryContext carrying turn 1's Q/A, and F7's
`render_memory_block` renders it into the rewrite prompt. `astream` is faked (no retrieval/OpenAI).
"""

import json
import uuid
from pathlib import Path

import pytest

import app.db.engine as db_engine
from app.api import ask
from app.memory import service
from app.rag import prompt

from .test_ask_memory import Recorder, _create_session, make_fake_astream

FOLLOWUPS = Path(__file__).resolve().parents[1] / "fixtures" / "memory" / "followups.jsonl"


def _load():
    return [json.loads(line) for line in FOLLOWUPS.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_dataset_has_ten_two_turn_dialogues():
    rows = _load()
    assert len(rows) == 10
    assert all(r["turn1"] and r["turn2"] and r["subject"] for r in rows)


@pytest.mark.parametrize("dialogue", _load(), ids=[d["id"] for d in _load()])
async def test_followup_carries_turn1_into_memory(client, authed, monkeypatch, dialogue):
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))

    sid = await _create_session(client, authed)

    # turn 1 — establishes the subject; drain the write-behind so the pair is complete for turn 2
    await client.post("/api/ask", json={"question": dialogue["turn1"], "session_id": str(sid)},
                      headers=authed["headers"])
    await service.drain_writes()

    # turn 2 — a bare follow-up; the pipeline must receive turn 1 in memory
    await client.post("/api/ask", json={"question": dialogue["turn2"], "session_id": str(sid)},
                      headers=authed["headers"])

    assert rec.memory is not None
    pair_texts = [p.content for p in rec.memory.pairs]
    assert dialogue["turn1"] in pair_texts  # turn-1 question carried into the window

    # and F7's shared renderer puts that history into the rewrite/generation prompt, so a follow-up
    # like "aur MPhil ka?" can be condensed against it (F7 owns the condensation itself)
    block = prompt.render_memory_block(rec.memory)
    assert dialogue["turn1"] in block
