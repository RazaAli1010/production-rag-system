"""T7 — the accept rule against REAL `text-embedding-3-small` vectors.

`tests/fixtures/cache/adversarial.jsonl` carries 18 hand-built pairs with their embeddings baked in
(embedded once, committed), so this runs offline in CI and the calibration cannot silently drift.

This is the test that decides whether the semantic tier is safe to enable. `test_store.py` proves
the RULE is wired correctly with synthetic vectors; this proves the SHIPPED THRESHOLDS separate real
questions.

What the calibration found (see docs/eval_results/f9-cache-after.md for the full write-up):

- The specced 0.95 threshold is unreachable — nothing in the set scores that high. At 0.95 the
  semantic tier never fires.
- The sets OVERLAP on cosine: the worst adversarial pair (15(3) vs 15(4), 0.930) outscores the best
  true paraphrase (0.912). No cosine threshold alone separates them.
- The discriminative veto rejects 7/10 adversarial pairs with 0 false vetoes, and is what makes a
  usable threshold possible at all.
- A Jaccard floor (specced) contributes NOTHING once the veto is in place, and was dropped.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from app.caching.keys import discriminators, normalize

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cache" / "adversarial.jsonl"


def _pairs():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _cosine(a, b) -> float:
    a, b = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def _accepts(pair, settings) -> bool:
    """The SHIPPED accept rule, applied exactly as `store._lookup` applies it."""
    if _cosine(pair["vec_a"], pair["vec_b"]) < settings.CACHE_SIMILARITY_THRESHOLD:
        return False
    terms = frozenset(settings.CACHE_DISCRIMINATIVE_TERMS)
    return (discriminators(normalize(pair["a"]), terms)
            == discriminators(normalize(pair["b"]), terms))


ADVERSARIAL = [p for p in _pairs() if p["label"] == "should_miss"]
PARAPHRASES = [p for p in _pairs() if p["label"] == "should_hit"]


def test_fixture_is_committed_and_populated():
    assert len(ADVERSARIAL) == 10
    assert len(PARAPHRASES) == 8
    assert all(len(p["vec_a"]) == 1536 for p in _pairs())


# ------------------------------------------------- the safety property (feature AC #2)

@pytest.mark.parametrize("pair", ADVERSARIAL, ids=lambda p: p["why"][:40])
def test_adversarial_pair_does_not_collide(pair, cache_settings):
    """No adversarial pair may be served from the other's cache entry at the shipped thresholds.
    A failure here is a wrong answer with real citations attached — the exact failure mode this
    project refuses to ship."""
    assert not _accepts(pair, cache_settings), (
        f"COLLISION: {pair['a']!r} would be answered from {pair['b']!r} "
        f"(cosine={_cosine(pair['vec_a'], pair['vec_b']):.3f}) — {pair['why']}"
    )


# --------------------------------------------------------------- the value property

def test_the_shipped_rule_keeps_the_paraphrases_it_claims_to():
    """2/8 at the shipped 0.86 — the honest measured number, pinned so a change in either
    direction is loud.

    2/8 is LOW. Three reasons, all real:
    - 3 of the 8 are code-switched and score 0.33-0.47 (see below) — unreachable at any safe floor.
    - "What is the HEC plagiarism policy?" / "What are the HEC rules on plagiarism?" scores 0.856,
      just under the floor. Dropping to 0.85 would recover it (3/8) at a 0.022 margin instead of
      0.032 — a deliberate trade that was declined; the margin is the scarcer resource here.
    - "How do I get off academic probation?" / "What is the process to clear academic probation?"
      scores 0.839, and 0.838 is where the nearest adversarial pair sits. Not worth 0.001.
    """
    kept = [p for p in PARAPHRASES if _accepts(p, _settings())]
    assert len(kept) == 2, [p["a"] for p in kept]


def _settings():
    from tests.cache.conftest import make_settings
    return make_settings(ENABLE_CACHE=True)


def test_code_switched_paraphrases_do_not_hit_and_that_is_a_known_limitation():
    """text-embedding-3-small does not map Urdu transliteration onto its English twin: measured
    0.33-0.47 cosine, far below any usable threshold.

    This matters for the product, not just the cache: the target user types exactly like this
    ("probation se kaise nikalta hoon"). It means the semantic tier delivers nothing for
    code-switched queries UNLESS F7's rewrite is on to normalize them to English first — and F7
    ships default-off. Documented here rather than discovered in prod.
    """
    settings = _settings()
    code_switched = [p for p in PARAPHRASES
                     if _cosine(p["vec_a"], p["vec_b"]) < 0.6]
    assert len(code_switched) == 3
    assert not any(_accepts(p, settings) for p in code_switched)


# --------------------------------------------------------------- why the rule is shaped this way

def test_cosine_alone_cannot_separate_the_sets():
    """The load-bearing finding: the sets overlap, so no cosine threshold exists that admits every
    paraphrase and rejects every adversarial pair. This is why the veto is not optional."""
    worst_adversarial = max(_cosine(p["vec_a"], p["vec_b"]) for p in ADVERSARIAL)
    best_paraphrase = max(_cosine(p["vec_a"], p["vec_b"]) for p in PARAPHRASES)
    assert worst_adversarial > best_paraphrase, (
        "the sets no longer overlap — re-run T7's calibration, a cosine-only rule may now be viable"
    )


def test_the_specced_095_threshold_would_never_fire():
    """Recorded so nobody 'restores' the specced default thinking it was the safe choice: at 0.95
    the semantic tier is dead code, not a conservative setting."""
    assert all(_cosine(p["vec_a"], p["vec_b"]) < 0.95 for p in _pairs())


def test_the_veto_never_rejects_a_true_paraphrase():
    """0 false vetoes across the paraphrase set — the veto costs no real cache value, so its only
    cost is the code."""
    terms = frozenset(_settings().CACHE_DISCRIMINATIVE_TERMS)
    for p in PARAPHRASES:
        assert (discriminators(normalize(p["a"]), terms)
                == discriminators(normalize(p["b"]), terms)), p["a"]


def test_the_veto_carries_most_of_the_separation():
    """6/10 adversarial pairs are rejected by the veto regardless of cosine. (It was 7 while `pu`/
    `hec` were in the term list; that 7th — PU vs HEC plagiarism penalty — is at 0.740 cosine and
    is rejected by the floor anyway, which is why those two terms were dropped. See
    `test_keys.test_issuing_bodies_are_deliberately_not_discriminators`.)"""
    terms = frozenset(_settings().CACHE_DISCRIMINATIVE_TERMS)
    vetoed = [p for p in ADVERSARIAL
              if discriminators(normalize(p["a"]), terms)
              != discriminators(normalize(p["b"]), terms)]
    assert len(vetoed) == 6


def test_the_margin_is_thin_and_that_is_why_the_cache_ships_default_off(cache_settings):
    """The surviving adversarial pairs have no discriminating token, so cosine alone holds them
    back. The gap between the shipped floor and the nearest one is the whole safety margin."""
    terms = frozenset(cache_settings.CACHE_DISCRIMINATIVE_TERMS)
    survivors = [_cosine(p["vec_a"], p["vec_b"]) for p in ADVERSARIAL
                 if discriminators(normalize(p["a"]), terms)
                 == discriminators(normalize(p["b"]), terms)]
    margin = cache_settings.CACHE_SIMILARITY_THRESHOLD - max(survivors)
    assert 0 < margin < 0.05, f"margin={margin:.3f}"
    assert cache_settings.ENABLE_CACHE is True  # the fixture turns it on; the DEFAULT is off:
    from app.core.settings import Settings
    assert Settings.model_fields["ENABLE_CACHE"].default is False
