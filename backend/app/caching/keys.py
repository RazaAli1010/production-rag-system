"""Cache key + lexical-guard primitives (design.md §4/§5).

Pure CPU, no I/O, no settings — deliberately: `normalize` is the ONE normalization both tiers key
on, so the Redis key and the Postgres `query_hash` can never disagree about what "the same
question" means.

`key_terms`/`discriminators` implement the lexical half of F9's two-signal accept rule. Cosine
similarity says *these two questions are about the same thing*; it does NOT say *these two questions
have the same answer*. "What does regulation 15(3) say?" and "What does regulation 15(4) say?" sit
at 0.930 cosine and have entirely different answers with different citations. Serving one for the
other is the hallucination-shaped failure this project refuses to ship, so a match must also survive
a lexical veto (design §5).

**Why a discriminative-token veto and not a Jaccard floor** (measured at T7 against real
`text-embedding-3-small` vectors — `tests/fixtures/cache/adversarial.jsonl`): the two sets OVERLAP
on cosine. The worst adversarial pair (15(3) vs 15(4), 0.930) scores HIGHER than the best true
paraphrase ("How do I apply for a transcript?", 0.912), so no cosine threshold separates them. A
Jaccard floor does not help either — the adversarial pairs are near-identical sentences, so they
score 0.4-0.67 Jaccard, comfortably ABOVE every floor that would still admit real paraphrases
(which score 0.125-0.667). The best (cosine, Jaccard) pair keeps 1 of 8 paraphrases.

What actually separates them is *which token differs*. Every adversarial pair swaps a token that
changes which document or row answers the question — a degree level, an issuing body, a year, a
section id. Vetoing on that disagreement rejects 7/10 adversarial pairs outright with 0 false
vetoes, and makes the Jaccard floor redundant (its optimal value falls to 0.0), so it is gone.

Async-mandate placement (CLAUDE.md "which side of the line"): sha256 over a short string and set
math over a handful of tokens are cheap pure-CPU, the same side of the line as F5's RRF and F7's
`rrf_merge`. Nothing here goes to `anyio.to_thread`.
"""

import hashlib
import re

# Stripped before comparison: they carry no topic information, so leaving them in inflates the
# Jaccard of every pair of English questions toward each other. NOT a general stopword list — it is
# deliberately tiny, because over-filtering is how the guard loses the tokens that discriminate.
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "than", "that", "this", "these", "those",
    "of", "in", "on", "at", "to", "for", "from", "by", "with", "about",
    "i", "me", "my", "you", "your", "it", "its", "do", "does", "did", "can",
    "how", "what", "when", "where", "which", "who", "whom", "why",
})

_TOKEN_RE = re.compile(r"[a-z0-9()./-]+")
_WS_RE = re.compile(r"\s+")

# Structural discriminators — intrinsic patterns, not a vocabulary an operator would edit, so they
# live here rather than in Settings (the editable word list is CACHE_DISCRIMINATIVE_TERMS).
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_SECTION_RE = re.compile(r"^\d+(\(\d+\))+$|^\d+\.\d+$")


def normalize(query: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation. Idempotent.

    Intentionally conservative: this is the CACHE KEY, so anything it throws away is a distinction
    two different questions can no longer be told apart by. F7's `normalized` has already done the
    real work (typo fixing, translation, condensing) — this only removes formatting noise so
    "How do I get off probation?" and "how do i get off probation" share one entry.
    """
    return _WS_RE.sub(" ", query.strip().lower()).strip(" ?!.,;:")


def exact_key(normalized: str, prefix: str = "") -> str:
    """`{prefix}{sha256hex}` — the Redis key, and (with no prefix) `cache_entries.query_hash`.

    Callers pass `settings.CACHE_KEY_PREFIX` for the Redis key so `--flush` can SCAN for it, and
    nothing for the Postgres column so the hash stays a pure function of the query.
    """
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def key_terms(normalized: str) -> frozenset[str]:
    """Content words for the lexical guard.

    Keeps SHORT and NUMERIC tokens (`bs`, `ms`, `15(3)`, `2024`) that a conventional
    stopword/length filter would drop — those are precisely the tokens that distinguish the
    adversarial pairs this guard exists to catch. A `min length >= 3` rule here would silently
    delete `bs` and hand back the BS/MPhil collision it is supposed to prevent.
    """
    return frozenset(t for t in _TOKEN_RE.findall(normalized) if t and t not in _STOPWORDS)


def discriminators(normalized: str, terms: frozenset[str]) -> frozenset[str]:
    """The tokens in `normalized` that change WHICH document or row answers the question, rather
    than merely how it is phrased. Two queries whose discriminator sets differ are about different
    things however similar they read (design §5).

    Three kinds, all measured as real collision sources at T7:
    - `terms` — the caller's `CACHE_DISCRIMINATIVE_TERMS`: degree levels and issuing bodies
      (bs/mphil/phd, pu/hec). "BS admission deadline" vs "MPhil admission deadline".
    - years — "2023 fee schedule" vs "2024 fee schedule" (0.897 cosine, different answer).
    - section ids — "regulation 15(3)" vs "15(4)" (0.930 cosine, the worst pair in the set). F7
      preserves these verbatim through rewrite for exactly this reason; the cache must not undo it.
    """
    found = set()
    for t in key_terms(normalized):
        if t in terms or _YEAR_RE.match(t) or _SECTION_RE.match(t):
            found.add(t)
    return frozenset(found)
