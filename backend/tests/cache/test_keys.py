from app.caching import keys


def test_normalize_is_idempotent():
    once = keys.normalize("  How do I get OFF probation?  ")
    assert keys.normalize(once) == once


def test_normalize_collapses_case_whitespace_and_trailing_punctuation():
    assert keys.normalize("  How   do I get off probation? ") == "how do i get off probation"
    assert keys.normalize("HOW DO I GET OFF PROBATION!!") == "how do i get off probation"


def test_normalize_keeps_internal_punctuation():
    # Section identifiers are the whole point of F7's "preserve 15(3) verbatim" rule — the cache key
    # must not be the place they get shredded.
    assert keys.normalize("What does Regulation 15(3) say?") == "what does regulation 15(3) say"


def test_exact_key_is_deterministic_and_collision_free_for_different_queries():
    a = keys.exact_key(keys.normalize("how do i get off probation"))
    b = keys.exact_key(keys.normalize("How do I get off probation?"))
    c = keys.exact_key(keys.normalize("what is the fee refund policy"))
    assert a == b  # same normalized query => same key
    assert a != c
    assert len(a) == 64  # sha256 hex


def test_exact_key_applies_prefix_for_redis_scan():
    key = keys.exact_key("q", prefix="campusrag:cache:")
    assert key.startswith("campusrag:cache:")
    # the un-prefixed form is what cache_entries.query_hash stores
    assert key.endswith(keys.exact_key("q"))


def test_key_terms_keeps_short_and_numeric_discriminators():
    """The load-bearing property: a length>=3 filter would drop `bs` and hand back the exact
    collision the lexical guard exists to prevent."""
    terms = keys.key_terms(keys.normalize("What is the BS admission deadline?"))
    assert "bs" in terms
    assert "admission" in terms
    assert "deadline" in terms

    assert "15(3)" in keys.key_terms(keys.normalize("What does regulation 15(3) say?"))
    assert "2024" in keys.key_terms(keys.normalize("What is the 2024 fee schedule?"))


def test_key_terms_drops_stopwords():
    terms = keys.key_terms(keys.normalize("What is the fee refund policy"))
    assert terms == frozenset({"fee", "refund", "policy"})


TERMS = frozenset({"bs", "mphil", "phd", "undergraduate", "postgraduate"})


def _disc(text: str) -> frozenset[str]:
    n = keys.normalize(text)
    return keys.discriminators(n, TERMS)


def test_degree_level_is_a_discriminator():
    assert _disc("What is the BS admission deadline?") == {"bs"}
    assert _disc("What is the MPhil admission deadline?") == {"mphil"}
    assert _disc("What is the BS admission deadline?") != _disc(
        "What is the MPhil admission deadline?"
    )


def test_issuing_bodies_are_deliberately_not_discriminators():
    """`pu`/`hec` were listed and removed (T15). They caused a REAL false veto — "...to sit PU
    exams" vs "...at Punjab University" are one question with two phrasings, but only the first
    yields a discriminator — and they caught nothing the cosine floor doesn't: the PU-vs-HEC
    plagiarism pair they targeted sits at 0.740, well under the 0.86 floor."""
    from app.core.settings import Settings

    shipped = Settings.model_fields["CACHE_DISCRIMINATIVE_TERMS"].default
    assert "pu" not in shipped and "hec" not in shipped
    assert _disc("What is the PU plagiarism penalty?") == frozenset()


def test_the_pu_abbreviation_paraphrase_that_motivated_removing_pu():
    """The live T15 pair: same question, two phrasings, must not disagree on discriminators."""
    a = _disc("What minimum percentage of classes must a student attend at Punjab University?")
    b = _disc("What is the minimum attendance required to sit PU exams?")
    assert a == b == frozenset()


def test_year_is_a_discriminator_without_being_listed():
    """Years are structural — an operator should not have to enumerate 2023, 2024, 2025... in
    CACHE_DISCRIMINATIVE_TERMS."""
    assert _disc("What is the 2023 fee schedule?") == {"2023"}
    assert _disc("What is the 2024 fee schedule?") == {"2024"}


def test_section_id_is_a_discriminator():
    """The worst real pair in the calibration set (0.930 cosine). F7 preserves section ids verbatim
    through rewrite for this reason; the cache must not undo that."""
    assert _disc("What does regulation 15(3) say?") == {"15(3)"}
    assert _disc("What does regulation 15(4) say?") == {"15(4)"}


def test_a_plain_paraphrase_has_no_discriminators():
    """The veto must be silent on questions that carry no discriminating token, or it would reject
    every real paraphrase along with the adversarial pairs."""
    assert _disc("How do I get off academic probation?") == frozenset()
    assert _disc("What is the process to clear academic probation?") == frozenset()


def test_agreeing_discriminators_do_not_veto():
    """Two questions that both say 'bs' are still cacheable against each other — the veto fires on
    DISAGREEMENT, not on presence."""
    assert _disc("What is the BS admission deadline?") == _disc(
        "When is the BS admission deadline?"
    )


def test_jaccard_is_gone():
    """A lexical Jaccard floor was specced and measured to contribute nothing once the
    discriminative veto is in place (its optimal value fell to 0.0) — see T7 and keys.py. Deleted
    rather than left as an inert knob that implies it is doing work."""
    assert not hasattr(keys, "jaccard")
