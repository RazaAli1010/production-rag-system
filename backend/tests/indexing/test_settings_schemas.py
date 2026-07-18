from app.core.contracts import Chunk
from app.core.settings import Settings
from app.indexing.schemas import IndexResult, Manifest, RunReport


def _settings(**over):
    base = dict(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
        OPENAI_API_KEY="sk-test",
        PINECONE_API_KEY="pc-test",
        PINECONE_INDEX="campus-rag",
    )
    base.update(over)
    return Settings(**base)


def test_settings_defaults_load():
    s = _settings()
    assert s.EMBED_MODEL == "text-embedding-3-small"
    assert s.EMBED_DIM == 1536
    assert s.EMBED_BATCH_SIZE == 100
    assert s.EMBED_CONCURRENCY == 4
    assert s.FIXED_CHUNK_TOKENS == 1000
    assert s.FIXED_CHUNK_OVERLAP == 200
    assert s.STRUCTURE_MAX_SECTION_TOKENS == 800
    assert s.PINECONE_METADATA_MAX_BYTES == 40_000
    assert s.OPENAI_API_KEY.get_secret_value() == "sk-test"
    assert s.PINECONE_API_KEY.get_secret_value() == "pc-test"


def test_settings_env_override():
    s = _settings(EMBED_CONCURRENCY=8, INDEXING_STRATEGY="structure")
    assert s.EMBED_CONCURRENCY == 8
    assert s.INDEXING_STRATEGY == "structure"


def test_chunk_roundtrip():
    c = Chunk(chunk_id="d:0", doc_id="d", seq=0, text="hi", token_count=1)
    assert Chunk.model_validate(c.model_dump()) == c
    assert c.section_heading is None and c.page_start is None and c.anchor is None


def test_report_models_roundtrip():
    r = IndexResult(
        doc_id="d", namespace="pu", chunk_count=3, tokens_in=10, cost_usd=0.001, status="indexed"
    )
    assert IndexResult.model_validate(r.model_dump()) == r
    m = Manifest(strategy="fixed", embed_model="text-embedding-3-small",
                 namespaces={"pu": {"vectors": 3, "chunks": 3}}, total_tokens=10,
                 est_cost_usd=0.001, created_at="2026-07-13T00:00:00Z")
    assert Manifest.model_validate_json(m.model_dump_json()) == m
    rr = RunReport(strategy="fixed", results=[r], skipped=[], total_tokens=10, total_cost_usd=0.001)
    assert rr.total_tokens == 10
