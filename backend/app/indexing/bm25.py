import pickle
import re

import anyio
from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"[\w؀-ۿ]+", re.UNICODE)


def urdu_safe_tokenize(text):
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _build_and_pickle_sync(texts, chunk_ids, path):
    corpus = [urdu_safe_tokenize(t) for t in texts]
    bm25 = BM25Okapi(corpus) if corpus else None
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({"bm25": bm25, "chunk_ids": chunk_ids}, f)
    return path


async def build_and_pickle(texts, chunk_ids, settings):
    return await anyio.to_thread.run_sync(
        _build_and_pickle_sync, texts, chunk_ids, settings.BM25_PATH
    )
