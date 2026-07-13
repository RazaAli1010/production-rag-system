from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "indexing"
EXPECTED = {"pdf_digital.jsonl", "html_page.jsonl", "docx_doc.jsonl",
            "pptx_deck.jsonl", "xlsx_sheet.jsonl", "pu_calendar.jsonl"}


def test_fixtures_present():
    assert EXPECTED.issubset({p.name for p in FIXTURES.glob("*.jsonl")})


def test_fixtures_small():
    total = sum(p.stat().st_size for p in FIXTURES.glob("*.jsonl"))
    assert total < 512 * 1024
