# Route evidence for Darwin-Route

`python -m arobi_integrations.route_evidence` turns public routing-benchmark rows into the content-free route
observations that Immaculate's Darwin-Route learns from.
- Darwin-Route lives in `apps/harness/src/darwin-route.ts` and `docs/architecture/DARWIN_ROUTE.md` in
  PossumXI/Immaculate.
- Each observation has the form `{taskClass, providerId, ok, latencyMs, costUsd, quality}`.

Darwin-Route is the single route-policy search. ASI-Evolve does not run a second one. This module only
supplies evidence that exists before live traffic does, for the models our providers actually serve.

```text
xRouteBench / RouterBench rows ──route_evidence──▶ benchmark-observations.jsonl (+ .manifest.json)
Immaculate route-outcome sink (live)  ───────────▶ YYYY-MM-DD.ndjson
                                                     │
          npm run darwin:route -- evolve --observations … --outcomes-dir … ──▶ report.json
          npm run darwin:route -- shadow (owner budget) ──▶ shadow.json
          npm run darwin:route -- table ──▶ route-order-table.json + sha256 ──▶ gateway (pinned)
```

## What carries over, and what does not

| field | value | why |
|---|---|---|
| `quality` | the item's task score (0–1) | measured by the benchmark on the same model |
| `costUsd` | measured tokens × **this provider's** per-1M price from the catalog | the benchmark's host price is not what we pay |
| `latencyMs` | `null` | the benchmark measured its host's latency, not our provider's |
| `ok` | `true` | a wrong answer is low quality, not a failed call |

**Model matching.** A provider gets rows only for the exact model it serves: `benchmarkModel` in the operator's
catalog. With Immaculate's default fallbacks, that means `router-groq`, `router-cerebras` and
`router-huggingface` for `gpt-oss-120b`. Other providers get no rows until live outcomes or shadow probes
measure them.

**Task classes.** Classes are Darwin-Route's: coding, research, reasoning, extraction, conversation.
- The rule that decided each benchmark task is recorded in the manifest.
- `--class-map` overrides any of them.

```json
{"providers": [
  {"providerId": "router-groq", "benchmarkModel": "gpt-oss-120b", "inputPer1M": 0.15, "outputPer1M": 0.75},
  {"providerId": "router-cloudflare", "benchmarkModel": null, "inputPer1M": 0, "outputPer1M": 0}
]}
```

These prices are illustrative. Use the operator's actual contract prices.

## Data rights

Neither dataset card declares a license: `ulab-ai/xRouteBench` (arXiv:2608.06867) and `withmartian/routerbench`
(arXiv:2403.12031). Immaculate's external-data rules default to DENY. Running this for real therefore waits on
the owner's license and intended-use decision, which is recorded in Immaculate's `CURRENT_HANDOFF.md`. The
intended use is seeding a routing prior, not training a model.

RouterBench ships as a pandas pickle, and unpickling executes code. The module refuses to open it unless its
sha256 matches `--expect-sha256`.

## Running it (local session with network access)

```bash
pip install pandas pyarrow
huggingface-cli download ulab-ai/xRouteBench --repo-type dataset --local-dir ~/data/xroutebench
python -m arobi_integrations.route_evidence xroutebench \
  --rows ~/data/xroutebench/llmrouter_generic/train.parquet \
  --rows ~/data/xroutebench/llmrouter_generic/test.parquet \
  --catalog provider_catalog.json --out benchmark-observations.jsonl
# then, in PossumXI/Immaculate:
npm run darwin:route -- evolve --observations benchmark-observations.jsonl \
  --outcomes-dir "$IMMACULATE_ROUTE_OUTCOMES_DIR" --out report.json
```

## Research notes (read from the Hugging Face Hub, 2026-09-27)

### Findings from large-scale routing benchmarks

- **Models are complementary, and no single model dominates.** LLMRouterBench (arXiv:2601.07206) covers
  400K+ instances, 21 datasets and 33 models, including GPT-5, Claude 4 and Gemini 2.5 Pro.
- **Most routers are indistinguishable under unified evaluation.** Per LLMRouterBench, several recent
  methods, including OpenRouter's commercial router, do not reliably beat the *best single model*, and do
  not cut cost without losing performance relative to it. Treat the best single candidate as the bar to
  clear, not the oracle.
- **The gap to the oracle is mostly model recall.** For many queries only one candidate answers correctly,
  and routers fail to pick it (LLMRouterBench). Gains come from recognizing the queries where a specific
  candidate is uniquely right.
- **Curation beats ensemble size.** Adding more models shows diminishing returns, while a carefully
  chosen subset does substantially better (LLMRouterBench). Dropping dominated candidates from an order is
  a legitimate policy move.
- **The choice of embedding backbone barely matters** (LLMRouterBench ablation).
- **Learned routers do beat fixed models when trained on real supervision.** Per LLMRouter / xRouteBench
  (arXiv:2608.06867):
  - they reach a 14.6% relative improvement over the strongest fixed model;
  - under tighter cost constraints the ranking reverses in favor of lightweight designs;
  - their cost-aware reward is `α·norm(performance) − β·norm(price_cost)`.
- **Routers collapse toward expensive models as the budget grows.** "When Routing Collapses"
  (arXiv:2602.03478) proposes learning direct model rankings (EquiRouter) instead, which lowers cost
  without losing performance. Watch for candidates that put the priciest provider first in every task class when
  the owner's `costPenaltyPerUsd` is small.
- **Output length is a routing lever too.** R2-Router (arXiv:2602.02823) selects the model and a length
  budget jointly. Our table orders providers only, so this is out of scope for now.
- **Router families** (as surveyed in arXiv:2608.06867):
  - binary strong/weak routers (RouteLLM, Hybrid LLM);
  - cost-aware cascades (FrugalGPT, AutoMix);
  - contrastive and graph routers (RouterDC, GraphRouter);
  - personalized and agentic routers (Router-R1).

  A cascade needs a second call. Here, the second entry in an order serves only when the first *fails*,
  so it is a fallback, not a cascade.

### Datasets and what they measure

- **xRouteBench** (`ulab-ai/xRouteBench`, Aug 2026; parquet): the primary source.
  - Every query was run against all 18 candidates, recording the response, task score (0–1), input and
    output tokens, and latency.
  - `llmrouter_generic` has 80,802 train rows (4,487 queries) and 67,122 test rows (3,729 queries) across
    13 classic benchmarks (MMLU, GSM8K, MATH, MBPP, ARC, …) with metrics em_mc, GSM8K, MATH, code_eval and
    f1.
  - Cost per row = `input_tokens × input_price/1e6 + output_tokens × output_price/1e6`.
  - The `*_queries` configs ship the raw queries, so new candidates (our providers) can be measured on the
    same items.
- **RouterBench** (`withmartian/routerbench`, arXiv:2403.12031): 30,000+ prompts from MBPP, GSM8K,
  Winogrande, Hellaswag, MMLU, MT-Bench and more. Each response has a correctness score and an estimated
  cost across 11 models (older generation: GPT-4, GPT-3.5, Claude v1/v2, Llama-2-70B, Mixtral, Yi-34B, …).
  It comes as a pickled pandas frame, so check the sha256 before loading.
- **LLMRouterBench** (`NPULH/LLMRouterBench`; results tarball `bench-release.tar.gz`, 1.28 GB): the 33-model,
  21-dataset source used for the findings above.

### xRouteBench candidate pool and published prices (USD per 1M tokens, input / output)

| candidate | size | input | output | served via |
|---|---|---|---|---|
| qwen2.5-7b-instruct | 7B | 0.20 | 0.20 | NVIDIA |
| gemma-2-9b-it | 9B | 0.10 | 0.10 | NVIDIA |
| llama-3-8b-instruct-lite | 8B | 0.10 | 0.10 | Together |
| qwen2.5-7b-instruct-turbo | 7B | 0.30 | 0.30 | Together |
| mistral-7b-instruct-v0.3 | 7B | 0.20 | 0.20 | NVIDIA |
| qwen3-next-80b-a3b-instruct | 80B (3B active) | 0.15 | 1.50 | Together |
| llama3-70b-instruct | 70B | 0.90 | 0.90 | NVIDIA |
| mixtral-8x7b-instruct-v0.1 | 46.7B | 0.60 | 0.60 | NVIDIA |
| mixtral-8x22b-instruct-v0.1 | 140.6B | 1.20 | 1.20 | NVIDIA |
| gpt-oss-20b | 20B | 0.05 | 0.20 | Together |
| mistral-small-3-24b-instruct | 24B | 0.10 | 0.30 | Together |
| llama-4-maverick | 402B | 0.27 | 0.85 | Together |
| rnj-1-instruct | 15B | 0.15 | 0.15 | Together |
| gpt-oss-120b | 120B | 0.15 | 0.60 | Together |
| qwen3-coder-next | 200B | 0.50 | 1.20 | Together |
| deepseek-v3.1 | 671B | 0.60 | 1.70 | Together |
| llama-3.3-70b-instruct-turbo | 70B | 0.88 | 0.88 | Together |
| cogito-v2-1-671b | 671B | 1.25 | 1.25 | Together |

