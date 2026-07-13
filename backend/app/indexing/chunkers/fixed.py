from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.contracts import Chunk
from app.indexing.chunkers.base import make_chunk_id, truncate_to_limit


class FixedChunker:
    def __init__(self, settings):
        self.settings = settings
        self.splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=settings.FIXED_CHUNK_TOKENS,
            chunk_overlap=settings.FIXED_CHUNK_OVERLAP,
        )

    def split(self, docs, doc_id):
        chunks = []
        seq = 0
        for doc in docs:
            md = doc.metadata
            for piece in self.splitter.split_text(doc.page_content):
                text, tc = truncate_to_limit(piece, self.settings)
                chunks.append(Chunk(
                    chunk_id=make_chunk_id(doc_id, seq), doc_id=doc_id, seq=seq, text=text,
                    section_heading=md.get("section_heading"),
                    page_start=md.get("page_start"), page_end=md.get("page_end"),
                    anchor=md.get("anchor"), token_count=tc,
                ))
                seq += 1
        return chunks
