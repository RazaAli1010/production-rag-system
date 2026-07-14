# Eval delta — `f5-hybrid-after` vs `baseline`

| metric | slice | baseline | f5-hybrid-after | Δ | dir |
|---|---|---|---|---|---|
| hit@1 | overall | 0.6190 | 0.6825 | +0.0635 | ▲ |
| hit@1 | code_switched | 0.6250 | 0.5625 | -0.0625 | ▼ |
| hit@1 | en | 0.6111 | 0.6944 | +0.0833 | ▲ |
| hit@1 | multi_doc | 0.5000 | 0.5000 | +0.0000 | = |
| hit@1 | table_lookup | 0.5625 | 0.6875 | +0.1250 | ▲ |
| hit@3 | overall | 0.9206 | 0.9048 | -0.0159 | ▼ |
| hit@3 | code_switched | 0.8750 | 0.8125 | -0.0625 | ▼ |
| hit@3 | en | 0.9167 | 0.9444 | +0.0278 | ▲ |
| hit@3 | multi_doc | 0.8333 | 1.0000 | +0.1667 | ▲ |
| hit@3 | table_lookup | 1.0000 | 0.8750 | -0.1250 | ▼ |
| hit@5 | overall | 0.9841 | 0.9206 | -0.0635 | ▼ |
| hit@5 | code_switched | 0.9375 | 0.8125 | -0.1250 | ▼ |
| hit@5 | en | 1.0000 | 0.9722 | -0.0278 | ▼ |
| hit@5 | multi_doc | 1.0000 | 1.0000 | +0.0000 | = |
| hit@5 | table_lookup | 1.0000 | 0.8750 | -0.1250 | ▼ |
| mrr | overall | 0.7717 | 0.7870 | +0.0153 | ▲ |
| mrr | code_switched | 0.7521 | 0.6667 | -0.0854 | ▼ |
| mrr | en | 0.7801 | 0.8171 | +0.0370 | ▲ |
| mrr | multi_doc | 0.7083 | 0.6944 | -0.0139 | ▼ |
| mrr | table_lookup | 0.7500 | 0.7812 | +0.0312 | ▲ |
