# Cognition seed: LLM routing (what is known to work, and what does not)

Sources were read from the Hugging Face Hub on 2026-09-27: dataset cards, the candidate pricing table, and
paper texts. Each claim names its source.

## Findings from large-scale routing benchmarks

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
  without losing performance. Watch for tables that put the priciest candidate first in every cell when
  `lambda_cost` is small.
- **Output length is a routing lever too.** R2-Router (arXiv:2602.02823) selects the model and a length
  budget jointly. Our table orders providers only, so this is out of scope for now.
- **Router families** (as surveyed in arXiv:2608.06867):
  - binary strong/weak routers (RouteLLM, Hybrid LLM);
  - cost-aware cascades (FrugalGPT, AutoMix);
  - contrastive and graph routers (RouterDC, GraphRouter);
  - personalized and agentic routers (Router-R1).

  A cascade needs a second call. Here, the second entry in an order serves only when the first *fails*,
  so it is a fallback, not a cascade.

## Datasets and what they measure

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

## xRouteBench candidate pool and published prices (USD per 1M tokens, input / output)

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

## How this maps onto Immaculate's provider pool

- **Default models of Immaculate's governed fallbacks** (`apps/harness/src/q-router-provider-adapters.ts`):
  - `router-groq`, `router-cerebras` and `router-huggingface` serve **gpt-oss-120b**, which xRouteBench
    measures directly.
  - `router-mistral` serves `mistral-small-latest`, `router-google` serves a Gemini Flash model, and
    `router-cloudflare` serves `gemma-4-26b-a4b-it`.
  - `router-openrouter` serves `openrouter/free`, whose model changes.
- **Only gpt-oss-120b is covered by public data.** The other providers' models are not the same as any
  xRouteBench candidate (for example, Mistral-Small-24B-2501 is not `mistral-small-latest`). Ranking them
  honestly requires measuring them on the xRouteBench raw queries (`npm run q:route-measure`, then
  `score_responses.py`). Until then, the table lists them as unmeasured and keeps their configured order.
- **Q (`q-horizon`) always serves first.** The table only orders the fallbacks behind it.
