import re
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.core.contracts import RetrievedChunk
from app.rag.baseline import build_generate_chain

RAG_DIR = Path(__file__).resolve().parents[2] / "app" / "rag"


def _chunk(i):
    return RetrievedChunk(chunk_id=f"d:{i}", doc_id="d", title="Title", text=f"body {i}",
                          dense_score=0.9)


async def test_generate_chain_streams_tokens_reassembling_full_answer():
    full_answer = "The probation policy states [1] that students must maintain a GPA."
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content=full_answer)]))
    chain = build_generate_chain(fake_llm)

    chunk_input = {"chunks": [_chunk(0)], "memory_block": "", "question": "probation policy?",
                   "language_directive": ""}

    tokens = []
    async for event in chain.astream_events(chunk_input, version="v2"):
        if event["event"] == "on_chat_model_stream":
            tokens.append(event["data"]["chunk"].content)

    assert "".join(tokens) == full_answer
    assert len(tokens) > 1  # actually streamed in multiple pieces, not one blob


async def test_generate_chain_driven_only_via_astream_events():
    # A direct .ainvoke() also works (AC-5 permits it), but astream() (T11) exclusively uses
    # astream_events for live token delivery — this test documents that astream_events alone is
    # sufficient to get the full answer.
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="answer text")]))
    chain = build_generate_chain(fake_llm)
    chunk_input = {"chunks": [_chunk(0)], "memory_block": "", "question": "q",
                   "language_directive": ""}

    collected = ""
    async for event in chain.astream_events(chunk_input, version="v2"):
        if event["event"] == "on_chain_end" and event.get("name") == "RunnableSequence":
            collected = event["data"]["output"]

    assert collected == "answer text"


def test_no_sync_invoke_or_stream_call_sites_in_app_rag():
    """In-suite grep-assertion (tasks.md T6/design.md §11) — mirrors the CI async-guard job so
    the guard is enforced even outside CI (e.g. local `pytest`)."""
    violations = []
    for path in RAG_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\.invoke\(", line) or re.search(r"\.embed_query\(", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
            # `.stream(` but not `.astream(`/`.astream_events(`
            if re.search(r"(?<!a)\.stream\(", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert violations == [], f"sync call sites found in app/rag/: {violations}"
