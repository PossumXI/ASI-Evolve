# Task: route Immaculate's governed fallbacks to the provider that does each kind of work best per dollar

You are improving `rank_candidates(cell, stats, params)` in a Python program. For each cell of work (a
TaskClass: retrieval, extraction, transform, reasoning, codegen, verification or conversation, crossed with
a ComplexityBand: trivial, low, moderate or high, plus the catch-all `*|*`), it returns the candidate
models in the order they should serve.

Fitness is measured, not estimated: the evaluator replays recorded outcomes (the task score each candidate
actually earned on each benchmark query, and what it cost) and serves every query of the search fold with
the first candidate its cell names. Fitness is the mean of `performance - lambda_cost * cost / cost_reference`.
The evaluator also reports the best single candidate and the oracle; beating the best single candidate
is the bar (LLMRouterBench found most published routers do not clear it reliably).

What you may use: only `cell`, `stats` (fit-fold means per candidate: global, per class, per cell, each
with n, performance, cost_usd, latency_s) and `params`. Do not read files, the network, or the clock, and
stay deterministic: the same inputs must give the same ranking.

Ideas worth trying: shrinkage of thin cells toward class and global estimates; confidence-aware estimates
(means with few samples are noisy); curating the candidate set (dropping dominated candidates improves the
fallback order); treating the second position as the fallback that serves when the first fails.

What the result is used for: the ranking is compiled into a data table that Immaculate's gateway loads
with a pinned sha256. It only reorders fallbacks the operator already configured; Q always stays first,
and every governance gate still applies. The program never runs in production.
