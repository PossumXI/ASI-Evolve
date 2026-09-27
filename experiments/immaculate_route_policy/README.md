# Immaculate route policy ("JEV" routing evolution)

This experiment evolves the policy that decides which governed fallback provider the Immaculate gateway
tries first for each kind of work. It uses the same Darwin loop as the other ASI-Evolve experiments:
learn → design → experiment → analyze, with island (MAP-Elites) sampling and a cognition store. Fitness
comes from replaying measured outcomes. Nothing is simulated.

The result is a data table, never code. The gateway loads it with a pinned sha256. It only reorders
fallbacks the operator already configured. Q always serves first, and every gate (non-goals, lanes,
budget, human gate) still applies. See `apps/harness/src/route-order-table.ts` in PossumXI/Immaculate.

## Pipeline

```text
benchmark or measured outcomes ──prepare.py──▶ data/{fit,search,holdout}.jsonl + manifest.json
                                                   │
initial_program (rank_candidates) ──evolve loop──▶ evaluator.py (replay on search: fitness)
                                                   │
best program ──compile_table.py (holdout gate + provider catalog)──▶ route-order-table.json + sha256
                                                   │
Immaculate: IMMACULATE_ROUTE_ORDER_TABLE_PATH + IMMACULATE_ROUTE_ORDER_TABLE_SHA256
```

**Cells.** A cell is one of the Arobi spine's TaskClasses × ComplexityBands (Asgard
`engineering-intelligence.ts`), plus the catch-all `*|*`.
- `route_data.complexity_band` mirrors the spine classifier's thresholds exactly.
- Benchmark tasks map to classes by documented rules. `prepare.py` records the rule that mapped each one
  in the manifest, and `--class-map` overrides any of them.

**Fitness.** Fitness is `mean(performance − λ · cost_usd / cost_reference)` over the search fold, taking
the outcome of the candidate the cell's order serves first.
- `cost_reference` is the fit fold's median non-zero cost.
- Every run reports:
  - the best single candidate, which is the bar (LLMRouterBench found most routers do not clear it);
  - the oracle and the oracle gap;
  - cost per 1K queries;
  - how the fallback performs in second position.

**Folds.** Folds split by query, so all candidates of a query stay in one fold.
- `fit`: the statistics the program sees.
- `search`: what the loop optimizes.
- `holdout`: scored only by `compile_table.py`. A table that loses to the best single candidate on the
  holdout is refused.

## Data sources

| source | what | how it enters |
|---|---|---|
| [ulab-ai/xRouteBench](https://huggingface.co/datasets/ulab-ai/xRouteBench) (arXiv:2608.06867) | 18 candidates. Per (query, model): task score, tokens, latency. Published prices. | `prepare.py xroutebench`. Its train/test split is kept, and test becomes the holdout. |
| [withmartian/routerbench](https://huggingface.co/datasets/withmartian/routerbench) (arXiv:2403.12031) | 11 older-generation candidates. Per-query score and cost. | `prepare.py routerbench --expect-sha256 …`. The pickle is refused unless its sha256 matches. |
| our providers | the Immaculate fallbacks (Groq, Cerebras, HF router, Mistral, Google, Cloudflare, OpenRouter) on xRouteBench raw queries | `npm run q:route-measure` (Immaculate), then `score_responses.py`, then `prepare.py measured` |

Only `gpt-oss-120b` (Groq, Cerebras and HF router defaults) appears in public benchmark data. Other
providers stay **unmeasured** until they are measured. The table lists them and keeps their configured
order; nothing is guessed. See `cognition_seed.md` for the research findings and the full candidate price
table.

**License gate.** Neither dataset card declares a license. Immaculate's external-data rules default to
DENY, so the owner must record a license and intended-use decision before benchmark data is used. The use
is fitting a routing table, not training a model.

## Running it (local session with network, API keys and compute)

The cloud container that built this cannot reach huggingface.co. A local session does these steps.

```bash
# 0. Data (record sha256 of every file you download)
huggingface-cli download ulab-ai/xRouteBench --repo-type dataset --local-dir ~/data/xroutebench

# 1. Public benchmark outcomes -> folds
pip install pandas pyarrow
python experiments/immaculate_route_policy/prepare.py xroutebench \
  --train ~/data/xroutebench/llmrouter_generic/train.parquet \
  --test ~/data/xroutebench/llmrouter_generic/test.parquet \
  --candidates ~/data/xroutebench/llm_candidates/train.parquet \
  --out experiments/immaculate_route_policy/data

# 2. Our own providers on the same queries (Immaculate checkout, gateway env with IMMACULATE_ROUTER_* keys)
python experiments/immaculate_route_policy/prepare.py export-queries \
  --queries ~/data/xroutebench/llmrouter_generic_queries/train.parquet --per-task 40 --out queries.jsonl
npm run q:route-measure -- --queries queries.jsonl --out responses.jsonl --delay-ms 1500
python experiments/immaculate_route_policy/score_responses.py \
  --queries queries.jsonl --responses responses.jsonl --prices prices.json --out measured_outcomes.jsonl
python experiments/immaculate_route_policy/prepare.py measured \
  --outcomes measured_outcomes.jsonl --out experiments/immaculate_route_policy/data-measured

# 3. Baseline, then preflight (the evolve skill). A person must confirm before any round runs.
python experiments/immaculate_route_policy/evaluator.py experiments/immaculate_route_policy/initial_program /tmp/baseline.json
mkdir -p .evolve_runs/immaculate-route-policy
cp experiments/immaculate_route_policy/run_spec.template.yaml .evolve_runs/immaculate-route-policy/run_spec.yaml
#    -> present the preflight summary; only the user sets approval.confirmed: true

# 4. Evolve (LLM calls go through the Immaculate Q gateway: config.yaml)
#    via the evolve skill's evolve-* scripts, or: python main.py --experiment immaculate_route_policy \
#        --eval-script experiments/immaculate_route_policy/eval.sh

# 5. Compile the winner for production
python experiments/immaculate_route_policy/compile_table.py <best_program> \
  --catalog provider_catalog.json --data-dir experiments/immaculate_route_policy/data-measured \
  --out route-order-table.json --run-id <run>
```

`prices.json` holds the operator's actual contract prices. A free tier is 0.

`provider_catalog.json` maps each Immaculate provider id to the candidate whose outcomes describe it. With
measured data, the candidate is the provider id itself. Providers without data get `null`.

## Tests

`python -m pytest tests/test_immaculate_route_policy.py` covers:
- the classifier parity and the class rules;
- fold stability and row validation;
- the evaluator, including a failed program getting a floor fitness;
- the compiler's holdout gate and catalog mapping;
- the xRouteBench and RouterBench adapters and the unpinned-pickle refusal;
- every scorer.

These tests use small hand-built scenarios; none of them are measurements.
