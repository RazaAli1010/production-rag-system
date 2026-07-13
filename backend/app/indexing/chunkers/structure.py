import re

from app.core.contracts import Chunk
from app.indexing.chunkers.base import count_tokens, make_chunk_id, truncate_to_limit
from app.indexing.chunkers.fixed import FixedChunker


class _Section:
    def __init__(self, heading, anchor, page_start, page_end):
        self.heading = heading
        self.anchor = anchor
        self.page_start = page_start
        self.page_end = page_end
        self.parts = []

    @property
    def text(self):
        return "\n".join(p for p in self.parts if p).strip()


class StructureChunker:
    def __init__(self, settings):
        self.settings = settings
        self._patterns = [re.compile(p) for p in settings.STRUCTURE_CLAUSE_PATTERNS]
        self._fixed = FixedChunker(settings)

    def split(self, docs, doc_id):
        if any(d.metadata.get("section_heading") for d in docs):
            sections = self._by_heading(docs)
        elif any(d.metadata.get("anchor") for d in docs):
            sections = self._by_anchor(docs)
        else:
            sections = self._by_clause(docs)
        return self._emit(self._merge_forward(sections), doc_id)

    def _by_heading(self, docs):
        sections = []
        for d in docs:
            md = d.metadata
            heading = md.get("section_heading")
            if not sections or sections[-1].heading != heading:
                sections.append(_Section(heading, md.get("anchor"),
                                          md.get("page_start"), md.get("page_end")))
            sections[-1].parts.append(d.page_content)
            sections[-1].page_end = md.get("page_end")
        return sections

    def _by_anchor(self, docs):
        sections = []
        for d in docs:
            md = d.metadata
            anchor = md.get("anchor")
            if not sections or sections[-1].anchor != anchor:
                sections.append(_Section(None, anchor, md.get("page_start"), md.get("page_end")))
            sections[-1].parts.append(d.page_content)
            sections[-1].page_end = md.get("page_end")
        return sections

    def _by_clause(self, docs):
        sections = []
        for d in docs:
            md = d.metadata
            anchor = md.get("anchor")
            page_start, page_end = md.get("page_start"), md.get("page_end")
            for line in d.page_content.splitlines():
                if self._opens(line):
                    sections.append(_Section(line.strip(), anchor, page_start, page_end))
                elif not sections:
                    sections.append(_Section(None, anchor, page_start, page_end))
                sections[-1].parts.append(line)
                sections[-1].page_end = page_end
        return sections

    def _opens(self, line):
        return any(p.search(line) for p in self._patterns)

    def _merge_forward(self, sections):
        merged = []
        carry = []
        for sec in sections:
            if len(sec.text) < self.settings.CLEAN_MIN_BLOCK_CHARS:
                carry.append(sec.text)
                continue
            if carry:
                sec.parts = carry + sec.parts
                carry = []
            merged.append(sec)
        if carry:
            if merged:
                merged[-1].parts.extend(carry)
            elif sections:
                last = sections[-1]
                fallback = _Section(last.heading, last.anchor, last.page_start, last.page_end)
                fallback.parts = list(carry)
                merged.append(fallback)
        return merged

    def _emit(self, sections, doc_id):
        chunks = []
        seq = 0
        for sec in sections:
            text = sec.text
            if not text:
                continue
            if count_tokens(text) > self.settings.STRUCTURE_MAX_SECTION_TOKENS:
                from langchain_core.documents import Document
                doc = Document(page_content=text, metadata={
                    "doc_id": doc_id, "page_start": sec.page_start, "page_end": sec.page_end,
                    "anchor": sec.anchor, "section_heading": sec.heading})
                for child in self._fixed.split([doc], doc_id):
                    chunks.append(child.model_copy(update={
                        "chunk_id": make_chunk_id(doc_id, seq), "seq": seq,
                        "section_heading": sec.heading}))
                    seq += 1
                continue
            body, tc = truncate_to_limit(text, self.settings)
            chunks.append(Chunk(chunk_id=make_chunk_id(doc_id, seq), doc_id=doc_id, seq=seq,
                                text=body, section_heading=sec.heading, page_start=sec.page_start,
                                page_end=sec.page_end, anchor=sec.anchor, token_count=tc))
            seq += 1
        return chunks
