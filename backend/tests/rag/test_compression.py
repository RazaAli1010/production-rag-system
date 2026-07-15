"""F8 context-compression unit + wiring tests (requirements.md §4).

No live model on the pure-function paths: dedupe/floor/budget are score+tiktoken only, and sentence
trimming is exercised with a `FakeCrossEncoder` (the F6 stand-in). The `_pipeline_events` wiring test
drives `baseline.astream` with the F3 streaming harness (fixed retrieve + fake LLM), asserting
compression runs on the non-refused set and the compressed list flows to context + citations.
"""

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.core.contracts import PipelineFlags, RetrievedChunk
from app.core.settings import Settings
from app.rag import baseline, compression
from app.rag import retriever as retriever_mod


def _settings(**o):
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="a@b.c",
        ADMIN_PASSWORD="x",
        OPENAI_API_KEY="k",
        PINECONE_API_KEY="k",
        PINECONE_INDEX="i",
        **o,
    )


def _rc(chunk_id, text="body text here", score=None, **o):
    return RetrievedChunk(chunk_id=chunk_id, doc_id="d", title="T", text=text,
                          rerank_score=score, **o)


class FakeCrossEncoder:
    def __init__(self, logit_of=None):
        self.logit_of = logit_of or {}
        self.calls = []

    def score(self, text_pairs):
        self.calls.append(list(text_pairs))
        return [self.logit_of.get(text, 0.0) for _q, text in text_pairs]


# --------------------------------------------------------------------------- dedupe (AC-4/AC-5)

def test_dedupe_collapses_near_duplicate_keeping_higher_score():
    dup = "the university requires a minimum cgpa of two point zero to avoid probation status here"
    a = _rc("a", text=dup, score=0.9)
    b = _rc("b", text=dup + " indeed", score=0.4)  # ~identical 5-grams, lower score
    c = _rc("c", text="a completely different clause about plagiarism and misconduct penalties now",
            score=0.8)
    kept, dropped = compression.dedupe([a, b, c], _settings())
    assert [x.chunk_id for x in kept] == ["a", "c"]  # b dropped as the lower-scored duplicate
    assert dropped == 1


def test_dedupe_below_threshold_keeps_both():
    a = _rc("a", text="admission deadline for the bs program is the fifteenth of august each year")
    b = _rc("b", text="the mphil thesis must be submitted within four years of enrolment overall")
    kept, dropped = compression.dedupe([a, b], _settings())
    assert [x.chunk_id for x in kept] == ["a", "b"] and dropped == 0


def test_dedupe_short_text_does_not_crash_and_respects_min():
    a = _rc("a", text="cgpa rule", score=0.9)
    b = _rc("b", text="cgpa rule", score=0.5)  # identical, <5 words → full word-set compare
    kept, _ = compression.dedupe([a, b], _settings(COMPRESSION_MIN_CHUNKS=2))
    assert len(kept) == 2  # would collapse, but MIN_CHUNKS=2 tops the dup back up


# ----------------------------------------------------------------------- relevance floor (AC-1/2/3)

def test_floor_drops_below_threshold():
    chunks = [_rc("a", score=0.9), _rc("b", score=0.5), _rc("c", score=0.1), _rc("d", score=0.05)]
    kept, dropped = compression.relevance_floor(chunks, _settings(COMPRESSION_SCORE_FLOOR=0.25))
    assert [x.chunk_id for x in kept] == ["a", "b"] and dropped == 2


def test_floor_tops_up_to_min_chunks():
    chunks = [_rc("a", score=0.9), _rc("b", score=0.1), _rc("c", score=0.05)]
    kept, _ = compression.relevance_floor(
        chunks, _settings(COMPRESSION_SCORE_FLOOR=0.25, COMPRESSION_MIN_CHUNKS=2)
    )
    assert [x.chunk_id for x in kept] == ["a", "b"]  # only a clears floor; b topped up by score


def test_floor_keeps_none_scored_chunk():
    chunks = [_rc("a", score=0.9), _rc("b", score=None), _rc("c", score=0.1)]
    kept, _ = compression.relevance_floor(chunks, _settings(COMPRESSION_SCORE_FLOOR=0.25))
    assert [x.chunk_id for x in kept] == ["a", "b"]  # None kept (no signal to floor), c dropped


# --------------------------------------------------------- token budget + sentence trim (AC-6/7/8/10)

async def test_budget_keeps_fitting_chunks_whole():
    chunks = [_rc(f"c{i}", text=f"short sentence number {i}.", score=0.9) for i in range(3)]
    kept, n = await compression.token_budget_fill("q", chunks, _settings(COMPRESSION_TOKEN_BUDGET=2200))
    assert [c.chunk_id for c in kept] == ["c0", "c1", "c2"] and n == 0  # all fit, nothing trimmed


async def test_budget_trims_overflow_chunk_and_drops_rest(monkeypatch):
    model = FakeCrossEncoder(logit_of={"keep me.": 5.0, "drop this filler.": -1.0})

    async def _get(_s):
        return model

    monkeypatch.setattr("app.rag.rerank.get_rerank_model", _get)
    big = _rc("big", text="keep me. drop this filler.", score=0.9)
    tail = _rc("tail", text="tail content.", score=0.8)
    # budget only fits the top sentence of `big`; `tail` is dropped.
    kept, n = await compression.token_budget_fill(
        "q", [big, tail], _settings(COMPRESSION_TOKEN_BUDGET=compression.count_tokens("keep me."),
                                    COMPRESSION_MIN_CHUNKS=1)
    )
    assert [c.chunk_id for c in kept] == ["big"]  # tail dropped after the overflow trim
    assert kept[0].text == "keep me."  # only the top-scored sentence survived
    assert n == 1 and len(model.calls) == 1  # one batched off-loop score call


async def test_trim_preserves_metadata_and_original_sentence_order(monkeypatch):
    middle = "very long filler sentence with many extra words padded out to inflate its token cost."
    model = FakeCrossEncoder(logit_of={"alpha.": 3.0, middle: -2.0, "omega.": 4.0})

    async def _get(_s):
        return model

    monkeypatch.setattr("app.rag.rerank.get_rerank_model", _get)
    text = f"alpha. {middle} omega."
    chunk = _rc("x", text=text, score=0.9, page_start=7, page_end=8, section_heading="Sec 3")
    budget = compression.count_tokens("alpha. omega.") + 3  # fits the two short facts, not `middle`
    trimmed, dropped = await compression._trim_chunk("q", chunk, budget, _settings())
    assert trimmed.text == "alpha. omega."  # top-2 kept, re-emitted in document order
    assert dropped == 1
    assert trimmed.chunk_id == "x" and trimmed.page_start == 7 and trimmed.page_end == 8
    assert trimmed.section_heading == "Sec 3" and trimmed.rerank_score == 0.9  # metadata preserved
    assert chunk.text == text  # input untouched (model_copy)


async def test_trim_noop_when_chunk_fits(monkeypatch):
    async def _boom(_s):
        raise AssertionError("model must NOT load when the chunk already fits")

    monkeypatch.setattr("app.rag.rerank.get_rerank_model", _boom)
    chunk = _rc("x", text="short enough.", score=0.9)
    out, dropped = await compression._trim_chunk("q", chunk, 9999, _settings())
    assert out is chunk and dropped == 0


# --------------------------------------------------------------------------- orchestrator (AC-12/13)

async def test_compress_chunks_reduces_tokens_and_logs_once(monkeypatch):
    logged = {}
    monkeypatch.setattr(compression.observability, "log_compression",
                        lambda **kw: logged.update(kw))
    dup = "identical overlapping clause that appears twice in two adjacent fixed window chunks here"
    chunks = [_rc("a", text=dup, score=0.9), _rc("b", text=dup, score=0.4),
              _rc("c", text="unique relevant clause about probation and cgpa thresholds", score=0.8),
              _rc("d", text="low relevance tail", score=0.05)]
    out = await compression.compress_chunks("q", chunks, _settings(COMPRESSION_SCORE_FLOOR=0.25))
    assert [c.chunk_id for c in out] == ["a", "c"]  # b deduped, d floored
    assert logged["tokens_after"] < logged["tokens_before"]
    assert logged["chunks_before"] == 4 and logged["chunks_after"] == 2  # dropped derived in the log fn


async def test_compress_chunks_empty_input_is_noop():
    assert await compression.compress_chunks("q", [], _settings()) == []


async def test_compress_chunks_fallback_on_exception(monkeypatch):
    warned = {}
    monkeypatch.setattr(compression.logger, "warning",
                        lambda evt, **kw: warned.update(event=evt, **kw))
    monkeypatch.setattr(compression, "dedupe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    chunks = [_rc("a", score=0.9), _rc("b", score=0.8)]
    out = await compression.compress_chunks("q", chunks, _settings())
    assert out is chunks  # uncompressed fallback, never raises (AC-13)
    assert warned["event"] == "rag.compression_failed"


# --------------------------------------------------------------------------- toggle overlay (AC-17)

def test_apply_flags_maps_compression_without_mutating():
    from app.rag import flags as flags_mod

    settings = _settings()
    overlaid = flags_mod.apply_flags(settings, PipelineFlags(compression=True))
    assert overlaid.ENABLE_COMPRESSION is True
    assert settings.ENABLE_COMPRESSION is False  # copy, not in-place


def test_parse_flags_accepts_compression():
    from app.evals.flags import parse_flags

    flags = parse_flags("hybrid=on,rerank=on,query_rewrite=on,compression=on")
    assert flags.compression is True and flags.cache is False  # cache still forced off


# --------------------------------------------------------------- _pipeline_events wiring (AC-9/11/16)

def _fake_llm(text):
    return lambda settings: GenericFakeChatModel(messages=iter([AIMessage(content=text)]))


async def _collect(agen):
    return [ev async for ev in agen]


async def test_compression_runs_on_pipeline_and_drives_citations(monkeypatch, session):
    pool = [_rc("c0", text="probation clause one.", score=0.9),
            _rc("c1", text="probation clause two.", score=0.8),
            _rc("c2", text="irrelevant filler.", score=0.05)]  # floored out

    async def _retrieve(*a, **k):
        return pool

    seen = {}
    real_compress = compression.compress_chunks

    async def _spy_compress(query, chunks, settings):
        seen["query"] = query
        seen["n_in"] = len(chunks)
        return await real_compress(query, chunks, settings)

    monkeypatch.setattr(retriever_mod, "retrieve", _retrieve)
    monkeypatch.setattr(baseline.compression_mod, "compress_chunks", _spy_compress)
    monkeypatch.setattr(baseline, "build_llm", _fake_llm("Per the rules [1][2]."))
    # The pipeline's `apply_flags` overlay drives ENABLE_* from the request flags (not settings);
    # rerank on so the refusal gate reads the calibrated rerank_score (F8 depends on F6). retrieve is
    # patched, so no cross-encoder actually loads. COMPRESSION_SCORE_FLOOR is a plain setting.
    flags = PipelineFlags(rerank=True, compression=True)
    settings = _settings(COMPRESSION_SCORE_FLOOR=0.25)

    events = await _collect(baseline.astream("how to exit probation?", flags=flags, session=session,
                                             settings=settings))
    citations = next(e for e in events if e.event == "citations").data["citations"]
    assert seen["query"] == "how to exit probation?" and seen["n_in"] == 3  # scoring query = raw (AC-9)
    cited_ids = {c["chunk_id"] for c in citations}
    assert "c2" not in cited_ids  # the floored chunk never reached the prompt/citations (AC-11)


async def test_compression_off_is_noop(monkeypatch, session):
    pool = [_rc("c0", text="clause one.", score=0.9), _rc("c1", text="filler.", score=0.01)]

    async def _retrieve(*a, **k):
        return pool

    monkeypatch.setattr(retriever_mod, "retrieve", _retrieve)
    monkeypatch.setattr(baseline.compression_mod, "compress_chunks",
                        lambda *a, **k: pytest.fail("compress_chunks called while flag off"))
    monkeypatch.setattr(baseline, "build_llm", _fake_llm("Answer [1]."))
    flags = PipelineFlags(rerank=True, compression=False)  # generates; gate reads rerank_score

    await _collect(baseline.astream("q", flags=flags, session=session,
                                    settings=_settings()))  # compress_chunks must not be called
