# Eval delta — `f7-rewrite-after` vs `f6-rerank-after`

| metric | slice | f6-rerank-after | f7-rewrite-after | Δ | dir |
|---|---|---|---|---|---|
| hit@1 | overall | 0.6825 | 0.6825 | +0.0000 | ▲ |
| hit@1 | code_switched | 0.6250 | 0.6875 | +0.0625 | ▲ |
| hit@1 | en | 0.7778 | 0.7778 | -0.0000 | ▼ |
| hit@1 | multi_doc | 0.5000 | 0.5000 | +0.0000 | = |
| hit@1 | table_lookup | 0.5625 | 0.5625 | +0.0000 | = |
| hit@3 | overall | 0.8571 | 0.8571 | +0.0000 | ▲ |
| hit@3 | code_switched | 0.7500 | 0.7500 | +0.0000 | = |
| hit@3 | en | 0.9444 | 0.9444 | +0.0000 | ▲ |
| hit@3 | multi_doc | 1.0000 | 1.0000 | +0.0000 | = |
| hit@3 | table_lookup | 0.8125 | 0.8125 | +0.0000 | = |
| hit@5 | overall | 0.9683 | 0.9365 | -0.0318 | ▼ |
| hit@5 | code_switched | 0.9375 | 0.8750 | -0.0625 | ▼ |
| hit@5 | en | 0.9722 | 0.9722 | +0.0000 | ▲ |
| hit@5 | multi_doc | 1.0000 | 1.0000 | +0.0000 | = |
| hit@5 | table_lookup | 1.0000 | 0.9375 | -0.0625 | ▼ |
| mrr | overall | 0.7907 | 0.7820 | -0.0087 | ▼ |
| mrr | code_switched | 0.7312 | 0.7438 | +0.0126 | ▲ |
| mrr | en | 0.8620 | 0.8620 | +0.0000 | ▲ |
| mrr | multi_doc | 0.7222 | 0.7222 | +0.0000 | ▲ |
| mrr | table_lookup | 0.7240 | 0.7083 | -0.0157 | ▼ |

## Notes

- **Baseline provenance:** `f6-rerank-after` values were seeded into the eval DB from the committed
  `docs/eval_results/f6-rerank-after.md` (4-decimal precision), not re-run — retrieval is
  deterministic, so this reproduces the canonical F6 numbers. A consequence: rows with |Δ| < 0.00005
  (shown as `±0.0000`) sit within that rounding, so treat their ▲/▼ arrows as **flat**.
- **Suite:** retrieval only (hit@k / MRR) — the same suite the committed `baseline` / `f5` / `f6`
  reports use, and the one that carries the headline metric. Cost/latency deltas are not applicable
  here (f6 has no committed ragas/latency baseline; the +1 `gpt-4o-mini` rewrite call/query is logged
  at runtime via `rag.llm_cost`/`rag.rewrite`).
- **Material finding (headline NOT met):** query rewrite **improves** `code_switched` hit@1
  (+0.0625) and MRR (+0.0126) — translating code-switched queries into English surfaces the correct
  chunk *higher* — but **regresses** `code_switched` hit@5 (−0.0625), overall hit@5 (−0.0318), and
  `table_lookup` hit@5 (−0.0625). The multi-query fan-out + union RRF-merge (cap
  `REWRITE_MERGED_TOP_K=12`) + rerank-against-`normalized` reorders some chunks that were in F6's
  top-5 out of the top-5. The `en` slice is unchanged (no regression).
- **Verdict:** the headline `code_switched` hit@5 target was not met on this 588-chunk corpus /
  65-record set — a real multi-query precision-vs-recall tradeoff, not a code defect (unit tests
  green). F7 ships behind `ENABLE_QUERY_REWRITE` (default **off**); enabling it should follow tuning,
  e.g. a larger `REWRITE_MERGED_TOP_K` so the single rerank sees more merged candidates, and/or
  weighting the `normalized` query above the variants in the RRF merge.
