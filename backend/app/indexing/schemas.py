from pydantic import BaseModel

from app.core.contracts import Chunk

__all__ = ["Chunk", "IndexResult", "RunReport", "Manifest"]


class IndexResult(BaseModel):
    doc_id: str
    namespace: str
    chunk_count: int
    tokens_in: int
    cost_usd: float
    truncated_chunks: int = 0
    status: str


class Manifest(BaseModel):
    strategy: str
    embed_model: str
    namespaces: dict[str, dict[str, int]]
    total_tokens: int
    est_cost_usd: float
    created_at: str


class RunReport(BaseModel):
    strategy: str
    results: list[IndexResult]
    skipped: list[str]
    total_tokens: int
    total_cost_usd: float
    manifest_path: str | None = None
