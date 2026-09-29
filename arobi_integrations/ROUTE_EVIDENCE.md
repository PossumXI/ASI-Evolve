# Route evidence for Darwin-Route

`python -m arobi_integrations.route_evidence` turns permissively licensed third-party routing data into the
content-free route observations that Immaculate's Darwin-Route learns from.
- Darwin-Route lives in `apps/harness/src/darwin-route.ts` and `docs/architecture/DARWIN_ROUTE.md` in
  PossumXI/Immaculate.
- Each observation has the form `{taskClass, providerId, ok, latencyMs, costUsd, quality}` (plus a `source`
  provenance string, which Darwin-Route ignores).

Darwin-Route is the single route-policy search. ASI-Evolve does not run a second one. This module only
supplies evidence that exists before live traffic does, for the models our providers actually serve.

```text
MANIFEST-SOURCES.json + DOWNLOADS.json (operator, pinned)
        │ licence gate ─▶ commit pin ─▶ sha256 pin
        ▼
pinned file ──route_evidence──▶ observations.jsonl (+ .manifest.json)          ──▶ darwin:route evolve
                           └──▶ evaluation.jsonl   (+ .manifest.json)          ──▶ evaluation reference only
Immaculate route-outcome sink (live, our own data) ──▶ YYYY-MM-DD.ndjson          ──▶ darwin:route evolve
          npm run darwin:route -- evolve --observations … --outcomes-dir … ──▶ report.json
          npm run darwin:route -- shadow (owner budget) ──▶ shadow.json
          npm run darwin:route -- table ──▶ route-order-table.json + sha256 ──▶ gateway (pinned)
```

## Licence rule

Owner decision, 2026-09-28, recorded by knight-af on PossumXI/ASI-Evolve#1:

- Third-party routing data may be used **only** under a permissive licence. The allowed SPDX ids are
  `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `CC-BY-4.0`, `CC0-1.0` and `Unlicense`.
- The licence must be verified from **both** the dataset card and a licence file in the repository. The source
  manifest records both. Both must be on the list, and they must agree. The repository file cannot be the card
  itself (`README.md`).
- The gate refuses a missing licence, a licence not on the list, an SPDX expression (`MIT OR …`), the ambiguous
  Hub id `bsd`, a card and a licence file that disagree, and a licence other than the one the adapter was pinned
  with.
- Our own data is always allowed. That covers Immaculate's route-outcome sink and the observations written by
  `darwin:route measure` and `shadow`. It goes to Darwin-Route directly and never passes through this module.

**Refused sources.** The live cards for `ulab-ai/xRouteBench`, `withmartian/routerbench` and
`NPULH/LLMRouterBench` declare no licence.
- Their adapters are removed.
- The `xroutebench`, `routerbench` and `llmrouterbench` commands only print the refusal and exit 2. They do not
  read any file (the RouterBench pickle is never opened).
- The licence gate refuses those repositories even when a manifest claims a licence for them.
- Lifting the refusal takes a code change after the owner re-verifies both the card and the repository files.

## Pinning: the manifests the operator passes

Nothing is fetched. The operator downloads each file, pins it, and passes both manifests plus the local copy.
The two manifests may be separate files or one file holding both lists. Either may also be a bare JSON list.

`MANIFEST-SOURCES.json` holds one entry per repository:

```json
{"sources": [
  {
    "repo": "DJLougen/eljefe-router-data",
    "commit": "3239a7df…(7 to 40 hex; the full id is preferred)",
    "license": {
      "card": "apache-2.0",
      "repoFile": {"path": "LICENSE", "spdx": "Apache-2.0", "sha256": "…optional…"}
    }
  }
]}
```

`DOWNLOADS.json` holds one entry per downloaded file:

```json
{"downloads": [
  {
    "repo": "DJLougen/eljefe-router-data",
    "commit": "3239a7df…",
    "path": "scored_gptoss120b.jsonl",
    "sha256": "…64 hex of the file as downloaded (the .gz itself for a gzip file)…",
    "bytes": 1234567,
    "rows": 997
  }
]}
```

A file is read only when all of the following hold:
1. The repository passes the licence gate.
2. The source commit starts with the revision the adapter was written for.
3. The download entry's commit agrees with the source commit.
4. The local file's sha256, and `bytes` when given, match the pin.

`rows` is optional. When given, it is checked against the number of records in the file before any row is
dropped. Each output manifest records the provenance:
- the repository, commit, path, sha256 and licence evidence;
- an attribution line (CC-BY-4.0 requires credit wherever the evidence is shared);
- the sha256 of both manifests and of the catalog.

## Sources and their rules

| command | pinned source | licence | file | becomes |
|---|---|---|---|---|
| `eljefe-router-data` | `DJLougen/eljefe-router-data@3239a7df` | Apache-2.0 | `scored_gptoss120b.jsonl` (997 rows) | observations for `router-groq`, `router-huggingface` and `router-cerebras` |
| `icl-router` | `lalalamdbf/ICL-Router@6f2aa6eb` | Apache-2.0 | `train_router.json` (29,272 rows) | observations only for a catalog provider serving that exact model; Llama rows evaluation-only |
| `dataset-a-routing` | `massaindustries/dataset-A-routing@48bfd5ce` | CC-BY-4.0 | `data/results/train.jsonl.gz` (5,505 rows) | evaluation reference only (the command has no `--out`) |

**eljefe-router-data.** Only the gpt-oss-120b (`frontier_*`) arm seeds observations. It seeds only
`router-groq`, `router-huggingface` and `router-cerebras`, each only while the catalog says it serves
gpt-oss-120b. Another provider serving the same model (Fireworks, for instance) is not seeded.

| field | value | why |
|---|---|---|
| `quality` | `frontier_score` (0–1) | measured on the same model |
| `ok` | `true` | a wrong answer is low quality, not a failed call |
| `latencyMs` | `null` | `frontier_latency_ms` was measured on Fireworks, not on these providers |
| `costUsd` | `frontier_input_tokens` × the provider's own input price + `frontier_output_tokens` × its own output price | `frontier_cost` is Fireworks' price and is never read; unknown tokens give `null` |

The `local_*` arm (gemma-4-E4B-it) goes to the evaluation output. So do gpt-oss-120b rows whose task has no
class. A row whose `frontier_score` is missing or outside 0–1 is dropped and counted.

**icl-router.** Every Meta Llama row is evaluation-only (`llama-licence-naming-clause`). The rule names
Llama-3.1, whose licence carries a naming clause for anything built from its outputs. The gate covers the whole
Llama family, which carries the same obligations.
- This holds even when a catalog provider serves that exact Llama model.
- Any other row becomes an observation only for catalog providers whose `servedModel` is that exact model:
  - `quality` is `is_correct_direct` (0 or 1);
  - `ok` is `true`;
  - `latencyMs` and `costUsd` are `null`, because neither latency nor token counts are recorded.
- Rows for models no provider serves go to the evaluation output.

**dataset-a-routing.** The dataset is an evaluation reference only: none of our fallbacks serves its models (the
`qwen`, `ds4` and `kimi` arms).
- Dropped from all output, with counts in the manifest:
  - the `_schema_anchor` row;
  - AIME and LiveCodeBench rows, because of upstream copyright. The rule names AIME-2025, and every AIME year
    carries the same copyright.
- Rows marked `gated` are kept (content-free) and counted.

**Task classes.** The owner's mapping onto Darwin-Route's classes:

| benchmark task | Darwin-Route class |
|---|---|
| `mbpp` | coding |
| `gsm8k`, `math500`, `mmlu_pro` | reasoning |
| `ifeval` | extraction (format-constrained instruction following) |

Names are compared lower-cased with punctuation removed, so `MMLU-Pro` is `mmlu_pro`. Any other task has no class
and never becomes an observation. The manifest records the deciding rule for every task seen, for example
`owner-map:mmlupro->reasoning`.

## Evaluation output

`--evaluation-out` holds rows that must never be route observations for a serving provider. Each line has this
form:

```json
{"schema": "arobi.route-evaluation.v1", "use": "evaluation-only", "reason": "llama-licence-naming-clause",
 "source": "lalalamdbf/ICL-Router@6f2aa6eb:train_router.json", "item": "2", "task": "gsm8k",
 "taskClass": "reasoning", "model": "Llama-3.1-8B-Instruct", "quality": 1.0}
```

- `reason` is one of `llama-licence-naming-clause`, `model-not-served-by-our-fallbacks` or
  `task-class-unmapped`.
- There is no `providerId` or `ok` field. Darwin-Route's observation loader therefore rejects the file if it is
  ever passed as `--observations`.
- The module refuses to write an evaluation record into the observation output, and the reverse.

Both outputs are content-free: no query, prompt or response text is written.

## Catalog

The operator's own price table: the model each provider serves, as its Immaculate adapter is configured, and its
per-1M-token prices.

```json
{"providers": [
  {"providerId": "router-groq", "servedModel": "openai/gpt-oss-120b", "inputPer1M": 0.15, "outputPer1M": 0.75},
  {"providerId": "router-cerebras", "servedModel": "gpt-oss-120b", "inputPer1M": 0.25, "outputPer1M": 0.69},
  {"providerId": "router-huggingface", "servedModel": "openai/gpt-oss-120b:fastest", "inputPer1M": 0.10, "outputPer1M": 0.50},
  {"providerId": "router-cloudflare", "servedModel": null, "inputPer1M": 0, "outputPer1M": 0}
]}
```

- Model names are compared canonically: the last path segment, without a `:variant` suffix, lower-cased. So
  `openai/gpt-oss-120b:fastest` is `gpt-oss-120b`.
- These prices are illustrative. Use the operator's actual contract prices.

## Running it (on the machine that holds the pinned files)

```bash
D=D:/arobi-data/route-evidence
python -m arobi_integrations.route_evidence eljefe-router-data \
  --sources $D/MANIFEST-SOURCES.json --downloads $D/DOWNLOADS.json \
  --file $D/<local copy of scored_gptoss120b.jsonl> --catalog provider_catalog.json \
  --out eljefe-observations.jsonl --evaluation-out eljefe-evaluation.jsonl
python -m arobi_integrations.route_evidence icl-router \
  --sources $D/MANIFEST-SOURCES.json --downloads $D/DOWNLOADS.json \
  --file $D/<local copy of train_router.json> --catalog provider_catalog.json \
  --evaluation-out icl-evaluation.jsonl        # add --out only if a catalog provider serves one of its models
python -m arobi_integrations.route_evidence dataset-a-routing \
  --sources $D/MANIFEST-SOURCES.json --downloads $D/DOWNLOADS.json \
  --file $D/<local copy of data/results/train.jsonl.gz> --evaluation-out dataset-a-evaluation.jsonl
# then, in PossumXI/Immaculate:
npm run darwin:route -- evolve --observations eljefe-observations.jsonl \
  --outcomes-dir "$IMMACULATE_ROUTE_OUTCOMES_DIR" --out report.json
```

A refusal prints `refused: …` on stderr, exits 2 and writes nothing. No third-party dependency is needed.

## Research notes (read from the Hugging Face Hub, 2026-09-27)

These are findings from published papers. They inform the policy objective. The datasets behind the refused
benchmarks are not used.

### Findings from large-scale routing benchmarks

- **Models are complementary, and no single model dominates.** LLMRouterBench (arXiv:2601.07206) covers
  400K+ instances, 21 datasets and 33 models.
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

### Refused datasets (no licence on the card)

- **xRouteBench** (`ulab-ai/xRouteBench`, arXiv:2608.06867): 18 candidates, including gpt-oss-120b, with task
  scores, tokens and latency per query.
- **RouterBench** (`withmartian/routerbench`, arXiv:2403.12031): 30,000+ prompts across 11 older models, shipped
  as a pandas pickle.
- **LLMRouterBench** (`NPULH/LLMRouterBench`): the 33-model, 21-dataset results tarball behind the findings
  above.
