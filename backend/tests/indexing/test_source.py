import json
import uuid

import structlog

from app.core.settings import Settings
from app.db.enums import DocumentStatus
from app.db.models.corpus import Document as DocRow
from app.indexing.source import indexed_targets, load_blocks


def _settings(tmp_path):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", EXTRACTED_DIR=tmp_path)


def _doc_id():
    return f"test-source-{uuid.uuid4().hex[:8]}"


def _make_doc(doc_id, source_org, status):
    return DocRow(
        doc_id=doc_id, title="Doc", source_org=source_org,
        url="https://example.com/x.pdf", file_type="pdf",
        version_label="2021", is_scanned=False, status=status,
    )


async def test_load_blocks_reads_documents(tmp_path):
    s = _settings(tmp_path)
    line = {"page_content": "Block one.", "metadata": {"doc_id": "d1", "page_start": 1,
            "page_end": 1, "anchor": None, "section_heading": "Intro"}}
    (tmp_path / "d1.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    docs = await load_blocks("d1", s)
    assert len(docs) == 1
    assert docs[0].page_content == "Block one."
    assert docs[0].metadata["section_heading"] == "Intro"
    assert docs[0].metadata["page_start"] == 1


async def test_load_blocks_missing_file_warns_returns_empty(tmp_path):
    s = _settings(tmp_path)
    with structlog.testing.capture_logs() as logs:
        docs = await load_blocks("nope", s)
    assert docs == []
    assert any(e["event"] == "indexing.source.missing" for e in logs)


async def test_indexed_targets_filters_by_status(session, tmp_path):
    s = _settings(tmp_path)
    extracted_id, indexed_id = _doc_id(), _doc_id()
    registered_id, downloaded_id, failed_id = _doc_id(), _doc_id(), _doc_id()
    session.add_all([
        _make_doc(extracted_id, "PU", DocumentStatus.extracted),
        _make_doc(indexed_id, "PU", DocumentStatus.indexed),
        _make_doc(registered_id, "PU", DocumentStatus.registered),
        _make_doc(downloaded_id, "PU", DocumentStatus.downloaded),
        _make_doc(failed_id, "PU", DocumentStatus.failed),
    ])

    rows = await indexed_targets(session, "all", s)

    returned_ids = {row.doc_id for row in rows}
    assert extracted_id in returned_ids
    assert indexed_id in returned_ids
    assert registered_id not in returned_ids
    assert downloaded_id not in returned_ids
    assert failed_id not in returned_ids


async def test_indexed_targets_filters_by_namespace_case_insensitive(session, tmp_path):
    s = _settings(tmp_path)
    pu_id, hec_id = _doc_id(), _doc_id()
    session.add_all([
        _make_doc(pu_id, "PU", DocumentStatus.extracted),
        _make_doc(hec_id, "HEC", DocumentStatus.extracted),
    ])

    rows = await indexed_targets(session, "pu", s)

    returned_ids = {row.doc_id for row in rows}
    assert returned_ids == {pu_id}


async def test_indexed_targets_namespace_all_returns_both(session, tmp_path):
    s = _settings(tmp_path)
    pu_id, hec_id = _doc_id(), _doc_id()
    session.add_all([
        _make_doc(pu_id, "PU", DocumentStatus.extracted),
        _make_doc(hec_id, "HEC", DocumentStatus.extracted),
    ])

    rows = await indexed_targets(session, "all", s)

    returned_ids = {row.doc_id for row in rows}
    assert {pu_id, hec_id} <= returned_ids
