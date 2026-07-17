"""T1 — F17 Settings block + MemoryContext contract additions (AC-34, design §5/§7)."""

from app.core.contracts import MemoryContext

from .conftest import make_settings


def test_memory_settings_defaults():
    s = make_settings()
    assert s.ENABLE_MEMORY is False  # default-off; f9-cache-after parity (AC-33)
    assert s.MEMORY_TOKEN_BUDGET == 50_000
    assert s.MEMORY_WINDOW_PAIRS == 5
    assert s.MEMORY_KEEP_LAST_PAIRS == 2
    assert s.MEMORY_SUMMARIZE_EVERY_PAIRS == 3
    assert s.MEMORY_SUMMARY_MAX_TOKENS == 600
    assert s.MEMORY_SUMMARY_MODEL == "gpt-4o-mini"
    assert s.MEMORY_SESSION_TITLE_MAX_CHARS == 60
    assert s.MEMORY_ANON_MAX_MESSAGES == 30
    assert s.MEMORY_ANON_TTL_DAYS == 7


def test_memorycontext_new_fields_are_additive():
    # Pre-F17 producers construct MemoryContext with no window fields; defaults must be inert.
    m = MemoryContext()
    assert m.window_pairs == 0
    assert m.effective_tokens == 0
    assert m.summarized is False
    assert m.pairs == []
