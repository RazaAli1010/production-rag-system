import pickle

from app.core.settings import Settings
from app.indexing.bm25 import build_and_pickle, urdu_safe_tokenize


def _settings(tmp_path):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", BM25_PATH=tmp_path / "bm25.pkl")


def test_urdu_words_kept_intact():
    toks = urdu_safe_tokenize("probation سے kaise")
    assert "سے" in toks
    assert "probation" in toks


def test_latin_lowercased():
    assert urdu_safe_tokenize("Probation RULES") == ["probation", "rules"]


async def test_pickle_roundtrip_aligned(tmp_path):
    s = _settings(tmp_path)
    texts = ["first chunk text", "second chunk text", "third"]
    ids = ["d:0", "d:1", "d:2"]
    path = await build_and_pickle(texts, ids, s)
    blob = pickle.loads(path.read_bytes())
    assert blob["chunk_ids"] == ids
    assert blob["bm25"].get_scores(urdu_safe_tokenize("first")).shape[0] == 3


async def test_empty_corpus_does_not_raise(tmp_path):
    s = _settings(tmp_path)
    path = await build_and_pickle([], [], s)
    blob = pickle.loads(path.read_bytes())
    assert blob["bm25"] is None
    assert blob["chunk_ids"] == []
