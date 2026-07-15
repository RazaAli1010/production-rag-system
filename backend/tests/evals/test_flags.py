"""T3 — flag parsing (cache always forced off; unknown key rejected)."""

import pytest

from app.evals.flags import parse_flags


def test_parse_basic():
    f = parse_flags("hybrid=on,rerank=off,query_rewrite=true")
    assert f.hybrid is True and f.rerank is False and f.query_rewrite is True


def test_cache_always_forced_off():
    assert parse_flags("cache=on").cache is False
    assert parse_flags("hybrid=on").cache is False
    assert parse_flags(None).cache is False


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown flag 'bogus'"):
        parse_flags("bogus=on")


def test_malformed_pair_rejected():
    with pytest.raises(ValueError, match="expected key=on/off"):
        parse_flags("hybrid")


def test_non_boolean_value_rejected():
    with pytest.raises(ValueError, match="non-boolean"):
        parse_flags("hybrid=maybe")


def test_empty_and_whitespace():
    assert parse_flags("").model_dump()["hybrid"] is False
    assert parse_flags(" hybrid=on , ").hybrid is True
