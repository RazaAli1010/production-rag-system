from pathlib import Path

from sqlalchemy import func, select

from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rag"
EXPECTED = {"documents.jsonl", "chunks.jsonl", "smoke_questions.jsonl"}


def test_fixtures_present():
    assert EXPECTED.issubset({p.name for p in FIXTURES.glob("*.jsonl")})


def test_fixtures_small():
    total = sum(p.stat().st_size for p in FIXTURES.glob("*.jsonl"))
    assert total < 512 * 1024


def test_corpus_has_at_least_two_pu_and_one_hec_doc(smoke_questions):
    import json

    docs = [json.loads(line) for line in (FIXTURES / "documents.jsonl").read_text().splitlines()]
    assert sum(1 for d in docs if d["source_org"] == "PU") >= 2
    assert sum(1 for d in docs if d["source_org"] == "HEC") >= 1


def test_smoke_questions_has_ten_plus_one_out_of_corpus_probe(smoke_questions):
    assert len(smoke_questions) == 11
    probes = [q for q in smoke_questions if q["out_of_corpus"]]
    non_probes = [q for q in smoke_questions if not q["out_of_corpus"]]
    assert len(probes) == 1
    assert len(non_probes) == 10
    assert all(q["expected_doc_ids"] for q in non_probes)
    assert all(not q["expected_doc_ids"] for q in probes)


def test_smoke_questions_mix_plain_and_code_switched_phrasing(smoke_questions):
    # A crude but sufficient signal: at least one question uses common Urdu/English code-switch
    # markers ("hai", "hoon", "kya", "kaise", "mein", "ke"), and at least one is plain English.
    urdu_markers = ("hai", "hoon", "kya", "kaise", "mein", "ke ", "kitn")
    has_code_switched = any(
        any(m in q["question"].lower() for m in urdu_markers) for q in smoke_questions
    )
    has_plain_english = any(
        not any(m in q["question"].lower() for m in urdu_markers) for q in smoke_questions
    )
    assert has_code_switched
    assert has_plain_english


async def test_fixtures_load_via_async_session(seeded_corpus):
    doc_count = await seeded_corpus.scalar(select(func.count()).select_from(DocRow))
    chunk_count = await seeded_corpus.scalar(select(func.count()).select_from(ChunkRow))
    assert doc_count >= 3
    assert chunk_count >= 6
