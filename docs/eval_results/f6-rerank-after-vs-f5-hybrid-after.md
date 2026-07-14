# Eval delta — `f6-rerank-after` vs `f5-hybrid-after`

| metric | slice | f5-hybrid-after | f6-rerank-after | Δ | dir |
|---|---|---|---|---|---|
| hit@1 | overall | 0.6825 | 0.6825 | +0.0000 | = |
| hit@3 | overall | 0.9048 | 0.8571 | -0.0477 | ▼ |
| hit@5 | overall | 0.9206 | 0.9683 | +0.0477 | ▲ |
| mrr | overall | 0.7870 | 0.7907 | +0.0037 | ▲ |
| hit@1 | en | 0.6944 | 0.7778 | +0.0834 | ▲ |
| hit@3 | en | 0.9444 | 0.9444 | +0.0000 | = |
| hit@5 | en | 0.9722 | 0.9722 | +0.0000 | = |
| mrr | en | 0.8171 | 0.8620 | +0.0449 | ▲ |
| hit@1 | code_switched | 0.5625 | 0.6250 | +0.0625 | ▲ |
| hit@3 | code_switched | 0.8125 | 0.7500 | -0.0625 | ▼ |
| hit@5 | code_switched | 0.8125 | 0.9375 | +0.1250 | ▲ |
| mrr | code_switched | 0.6667 | 0.7312 | +0.0645 | ▲ |
| hit@1 | multi_doc | 0.5000 | 0.5000 | +0.0000 | = |
| hit@3 | multi_doc | 1.0000 | 1.0000 | +0.0000 | = |
| hit@5 | multi_doc | 1.0000 | 1.0000 | +0.0000 | = |
| mrr | multi_doc | 0.6944 | 0.7222 | +0.0278 | ▲ |
| hit@1 | table_lookup | 0.6875 | 0.5625 | -0.1250 | ▼ |
| hit@3 | table_lookup | 0.8750 | 0.8125 | -0.0625 | ▼ |
| hit@5 | table_lookup | 0.8750 | 1.0000 | +0.1250 | ▲ |
| mrr | table_lookup | 0.7812 | 0.7240 | -0.0572 | ▼ |
