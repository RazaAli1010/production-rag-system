"""T10: HTML loader yields anchors on headings, no nav text; link-only fixture flags a report
suggestion (AC-15, AC-32)."""

import pytest

from app.core.settings import Settings
from app.ingestion.loaders.html import load_html


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
    )


SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<nav><ul><li><a href="/">Home</a></li><li><a href="/about">About</a></li></ul></nav>
<header>Site Header Banner</header>
<h1 id="intro">Introduction</h1>
<p>This is the intro paragraph with real content about PU regulations.</p>
<h2>Section One</h2>
<p>Section one content goes here, describing academic probation rules.</p>
<footer>Copyright 2026 PU</footer>
</body>
</html>
"""

LINK_ONLY_HTML = """<!DOCTYPE html>
<html><body>
<h1>Notice</h1>
<p>See <a href="/files/policy.pdf">the policy PDF</a>.</p>
</body></html>
"""


@pytest.mark.asyncio
async def test_html_loader_yields_anchors_and_strips_nav(tmp_path):
    path = tmp_path / "sample.html"
    path.write_text(SAMPLE_HTML, encoding="utf-8")

    blocks = await load_html(path, "doc-html", _settings())

    joined = " ".join(b.page_content for b in blocks)
    assert "Home" not in joined
    assert "About" not in joined
    assert "Site Header Banner" not in joined
    assert "Copyright 2026 PU" not in joined

    intro_block = next(b for b in blocks if "intro paragraph" in b.page_content)
    assert intro_block.metadata["anchor"] == "intro"
    assert intro_block.metadata["section_heading"] == "Introduction"

    section_block = next(b for b in blocks if "academic probation" in b.page_content)
    assert section_block.metadata["anchor"] == "section-one"  # generated slug (no id attr)
    assert section_block.metadata["section_heading"] == "Section One"

    assert all(b.metadata["doc_id"] == "doc-html" for b in blocks)
    assert all(b.metadata["html_links_only_pdf"] is False for b in blocks)


@pytest.mark.asyncio
async def test_html_link_only_pdf_flagged_for_report(tmp_path):
    path = tmp_path / "link_only.html"
    path.write_text(LINK_ONLY_HTML, encoding="utf-8")

    blocks = await load_html(path, "doc-link-only", _settings())

    assert blocks
    assert any(b.metadata["html_links_only_pdf"] for b in blocks)
