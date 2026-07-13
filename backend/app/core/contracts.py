from pydantic import BaseModel


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    seq: int
    text: str
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    anchor: str | None = None
    token_count: int
