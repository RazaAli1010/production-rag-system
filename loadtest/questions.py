"""Question pools for the F15 load test.

Questions are drawn from the F4 eval dataset rather than generated, so the corpus can actually
answer them. Random strings would measure the refusal path at 50 concurrent users, which is not
the thing under test.

The split is the whole point of the 80/20 mix in the brief:

* **repeat pool** — a small fixed set asked 80% of the time. These are what the F9 cache is
  supposed to serve, so the run's cache hit rate is a property of this pool's size, not an
  accident.
* **novel pool** — asked 20% of the time, each question suffixed with a per-request nonce so it
  can never hit either cache tier. Without the nonce a "novel" question becomes a repeat the
  second time any of the 50 users picks it.
"""

import json
import pathlib
import random

DATASET = (
    pathlib.Path(__file__).resolve().parents[1]
    / "backend" / "app" / "data" / "evals" / "qa_dataset.jsonl"
)

REPEAT_POOL_SIZE = 12
REPEAT_PROBABILITY = 0.8


def _load() -> list[dict]:
    if not DATASET.exists():
        raise SystemExit(f"eval dataset not found at {DATASET} — the load test needs it for "
                         f"answerable questions")
    with DATASET.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _answerable(records: list[dict]) -> list[str]:
    # out_of_corpus records are designed to be refused. A refusal skips generation entirely, so
    # including them would quietly lower the measured p95 by measuring less work.
    return [r["question"] for r in records if "out_of_corpus" not in r.get("tags", [])]


_ALL = _answerable(_load())
# Deterministic split: every user class and every run picks the SAME repeat pool, so cache hit
# rate is comparable between runs on the same corpus.
_rng = random.Random(1510)
_SHUFFLED = _ALL[:]
_rng.shuffle(_SHUFFLED)

REPEAT_POOL = _SHUFFLED[:REPEAT_POOL_SIZE]
NOVEL_POOL = _SHUFFLED[REPEAT_POOL_SIZE:] or REPEAT_POOL


def pick(rng: random.Random) -> tuple[str, bool]:
    """Return `(question, is_repeat)` honouring the 80/20 mix."""
    if rng.random() < REPEAT_PROBABILITY:
        return rng.choice(REPEAT_POOL), True
    # The nonce is what makes "novel" true. It rides as a trailing clause so the question stays
    # grammatical and still retrieves sensibly — a raw uuid appended to the text would change what
    # the retriever sees far more than a cache-busting suffix should.
    q = rng.choice(NOVEL_POOL)
    return f"{q} (ref {rng.randrange(10**9)})", False


# 3-turn conversation for the F17 session cohort. Turn 2 and 3 are deliberately elliptical
# follow-ups — they are only answerable if memory + F7 condensation are actually working.
CONVERSATION = [
    "What is the minimum attendance required to sit the final examination?",
    "And what happens if I fall below it?",
    "Can that be waived by the department?",
]


if __name__ == "__main__":  # python questions.py — the mix is the thing that must not silently rot
    _r = random.Random(0)
    _picks = [pick(_r) for _ in range(200)]
    _repeat_share = sum(1 for _, is_repeat in _picks if is_repeat) / len(_picks)
    assert 0.72 <= _repeat_share <= 0.88, f"repeat share {_repeat_share} is not ~80%"

    _novel = [q for q, is_repeat in _picks if not is_repeat]
    # The nonce is the whole reason novel questions are novel — without it the 20% arm turns into
    # extra cache hits and the measured hit rate is silently wrong.
    assert len(set(_novel)) == len(_novel), "novel questions repeated — the nonce is not working"
    assert len(set(q for q, r in _picks if r)) <= REPEAT_POOL_SIZE

    assert all("out_of_corpus" not in q for q in REPEAT_POOL)
    print(f"ok — repeat share {_repeat_share:.2f}, {len(REPEAT_POOL)} repeat / "
          f"{len(NOVEL_POOL)} novel questions")
