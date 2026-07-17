"""T2 — tiktoken counting is exact and inline (AC-13, edge case "token counter drift")."""

import tiktoken

from app.memory import tokens


def test_empty_is_zero():
    assert tokens.count("") == 0


def test_matches_raw_encoder():
    enc = tiktoken.get_encoding("cl100k_base")
    for text in ["probation se kaise nikalta hoon", "BS admission deadline?", "aur MPhil ka?"]:
        assert tokens.count(text) == len(enc.encode(text))


def test_nonempty_is_positive():
    assert tokens.count("hello world") > 0
