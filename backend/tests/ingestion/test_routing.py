"""T7: each of {pdf, html, docx, pptx, xlsx} maps to expected callable; unknown -> raises."""

import pytest

from app.ingestion.loaders.html import load_html
from app.ingestion.loaders.office import load_office
from app.ingestion.loaders.pdf import load_pdf
from app.ingestion.routing import UnknownFileTypeError, select_loader


def test_pdf_routes_to_load_pdf():
    assert select_loader("pdf") is load_pdf


def test_html_routes_to_load_html():
    assert select_loader("html") is load_html


@pytest.mark.parametrize("file_type", ["docx", "pptx", "xlsx"])
def test_office_types_route_to_load_office(file_type):
    loader = select_loader(file_type)
    assert loader.func is load_office
    assert loader.keywords == {"file_type": file_type}


def test_unknown_file_type_raises():
    with pytest.raises(UnknownFileTypeError, match="csv"):
        select_loader("csv")
