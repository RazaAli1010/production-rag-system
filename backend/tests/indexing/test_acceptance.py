import pickle
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.settings import settings as global_settings
from app.db.engine import get_sessionmaker
from app.db.enums import DocumentStatus
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.indexing import run as run_module
from tests.indexing.test_run import FakeEmbeddings, FakeIndex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "indexing"


async def _seed(doc_id, org, file_type="pdf"):
    async with get_sessionmaker()() as session:
        session.add(DocRow(doc_id=doc_id, title=f"T-{doc_id}", source_org=org, url="http://x",
                           file_type=file_type, version_label="v1", is_scanned=False,
                           status=DocumentStatus.extracted))
        await session.commit()


def _install(doc_id, fixture):
    dst = global_settings.EXTRACTED_DIR / f"{doc_id}.jsonl"
    dst.write_text((FIXTURES / fixture).read_text(encoding="utf-8"), encoding="utf-8")


async def test_end_to_end_counts_manifest_cost(tmp_index_dirs, capsys):
    await _seed("pu-calendar-2021", "PU")
    _install("pu-calendar-2021", "pu_calendar.jsonl")
    idx = FakeIndex()
    await run_module.main(argv=["--strategy", "structure", "--namespace", "pu"],
                          index=idx, embeddings=FakeEmbeddings())
    async with get_sessionmaker()() as check:
        rows = (await check.execute(select(ChunkRow))).scalars().all()
    assert len(idx.vectors["pu"]) == len(rows)
    assert global_settings.INDEX_MANIFEST_PATH.exists()
    with_heading = [r for r in rows if r.section_heading]
    assert len(with_heading) / len(rows) >= 0.60
    assert "$" in capsys.readouterr().out
    blob = pickle.loads(global_settings.BM25_PATH.read_bytes())
    assert blob["chunk_ids"] == [r.chunk_id for r in sorted(rows, key=lambda x: (x.doc_id, x.seq))]


async def test_strategy_change_forces_wipe(tmp_index_dirs):
    await _seed("pu-calendar-2021", "PU")
    _install("pu-calendar-2021", "pu_calendar.jsonl")
    await run_module.main(argv=["--strategy", "fixed", "--namespace", "pu"],
                          index=FakeIndex(), embeddings=FakeEmbeddings())
    with pytest.raises(SystemExit):
        await run_module.main(argv=["--strategy", "structure", "--namespace", "pu"],
                              index=FakeIndex(), embeddings=FakeEmbeddings())
    report = await run_module.main(
        argv=["--strategy", "structure", "--namespace", "pu", "--wipe"],
        index=FakeIndex(), embeddings=FakeEmbeddings(),
    )
    assert report.strategy == "structure"
