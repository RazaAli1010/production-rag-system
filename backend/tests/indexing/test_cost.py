import pytest

from app.indexing.cost import estimate_cost


def test_embedding_cost():
    assert estimate_cost("text-embedding-3-small", 1_000_000) == pytest.approx(0.02)


def test_gpt4o_mini_in_and_out():
    got = estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert got == pytest.approx(0.15 + 0.60)


def test_gpt4o():
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000) == pytest.approx(2.50 + 10.0)


def test_unknown_model_raises():
    with pytest.raises(ValueError):
        estimate_cost("mystery", 10)
