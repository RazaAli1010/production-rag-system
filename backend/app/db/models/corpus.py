"""`documents`, `chunks` — mirror `DocumentMeta` / `Chunk` (design.md §3.3).

`RetrievedChunk` (dense/sparse/fused/rerank scores) is a **transient** runtime model, not
persisted — scores are recomputed per query, so there are deliberately no columns for them.
"""

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import DocumentStatus
from app.db.types import TZDateTime


class Document(Base):  # mirrors DocumentMeta + status
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(primary_key=True)  # slug+year, hec-plagiarism-policy-2021
    title: Mapped[str]
    source_org: Mapped[str]  # "PU" | "HEC" (CHECK)
    url: Mapped[str]
    file_type: Mapped[str]  # pdf|html|docx|pptx|xlsx (CHECK)
    downloaded_at: Mapped[TZDateTime]
    version_label: Mapped[str]
    is_scanned: Mapped[bool]
    page_count: Mapped[int | None]
    sha256: Mapped[str] = mapped_column(index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.registered
    )

    __table_args__ = (
        CheckConstraint("source_org IN ('PU', 'HEC')", name="source_org_valid"),
        CheckConstraint(
            "file_type IN ('pdf', 'html', 'docx', 'pptx', 'xlsx')", name="file_type_valid"
        ),
    )


class Chunk(Base):  # mirrors Chunk contract
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(primary_key=True)  # {doc_id}:{chunk_seq}
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id", ondelete="CASCADE"))
    seq: Mapped[int]
    text: Mapped[str]
    section_heading: Mapped[str | None]
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]
    anchor: Mapped[str | None]  # HTML anchor / slide no. / sheet name
    token_count: Mapped[int]

    __table_args__ = (Index("ix_chunks_doc_id_seq", "doc_id", "seq"),)  # AC-3.3
