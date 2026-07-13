import asyncio

import structlog
import tenacity

from app.indexing.cost import estimate_cost

logger = structlog.get_logger(__name__)


def _batch(items, n):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _is_rate_limit(exc):
    if getattr(exc, "status_code", None) == 429:
        return True
    return exc.__class__.__name__ in {"RateLimitError", "APIStatusError", "APITimeoutError"}


async def embed_chunks(chunks, embeddings, gate, settings):
    batches = list(_batch(chunks, settings.EMBED_BATCH_SIZE))
    results = await asyncio.gather(
        *(_embed_batch(b, embeddings, gate, settings) for b in batches)
    )
    vectors = []
    for group in results:
        vectors.extend(group)
    return vectors


async def _embed_batch(batch, embeddings, gate, settings):
    texts = [c.text for c in batch]
    tokens_in = sum(c.token_count for c in batch)
    async for attempt in tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception(_is_rate_limit),
        wait=tenacity.wait_exponential(min=1, max=30),
        stop=tenacity.stop_after_attempt(settings.EMBED_MAX_RETRIES),
        reraise=True,
    ):
        with attempt:
            async with gate:
                vectors = await embeddings.aembed_documents(texts)
    cost = estimate_cost(settings.EMBED_MODEL, tokens_in)
    logger.info("indexing.embed.batch", n=len(batch), tokens_in=tokens_in, est_cost_usd=cost)
    return vectors
