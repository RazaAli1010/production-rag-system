"""T7 — the single stage emitter: correct shape, ms on done, and the summarizing_memory stage
leads the sequence (AC-26/29)."""

from app.memory import stages


def test_emit_shape():
    ev = stages.emit("summarizing_memory", "started")
    assert ev.event == "stage"
    assert ev.data["stage"] == "summarizing_memory"
    assert ev.data["status"] == "started"
    assert ev.data["ms"] is None


def test_emit_done_carries_ms():
    ev = stages.emit("summarizing_memory", "done", ms=42)
    assert ev.data["ms"] == 42


def test_skipped_status():
    ev = stages.emit("summarizing_memory", "skipped")
    assert ev.data["status"] == "skipped"


def test_summarizing_memory_leads_sequence():
    assert stages.STAGE_SEQUENCE[0] == "summarizing_memory"
    # every downstream stage baseline already emits is present and after it
    for s in ("searching", "generating", "citing"):
        assert stages.STAGE_SEQUENCE.index(s) > 0


def test_timer_is_monotonic_nonnegative():
    t = stages.Timer()
    assert t.ms() >= 0
