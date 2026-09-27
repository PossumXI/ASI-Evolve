"""Public routing-benchmark evidence for Immaculate's Darwin-Route, as content-free route observations.

    python -m arobi_integrations.route_evidence xroutebench --rows llmrouter_generic/train.parquet \
        [--rows llmrouter_generic/test.parquet] --catalog provider_catalog.json --out benchmark-observations.jsonl
    python -m arobi_integrations.route_evidence routerbench --pickle routerbench_0shot.pkl \
        --expect-sha256 <hex> --catalog provider_catalog.json --out benchmark-observations.jsonl

Darwin-Route (PossumXI/Immaculate apps/harness/src/darwin-route.ts) is the one route-policy search. It learns
per task class x provider from route observations: {taskClass, providerId, ok, latencyMs, costUsd, quality}.
Live evidence comes from Immaculate's route-outcome sink and shadow evaluator. This module adds evidence that
exists before any live traffic does: public benchmark rows for the models our providers actually serve.
Feed the output to `npm run darwin:route -- evolve --observations <file>`.

Only measured facts are carried over:
  * quality   the task score the model earned on the benchmark item (0-1);
  * costUsd   the item's measured input/output tokens times THIS provider's own per-1M price (from the
              operator's catalog), because the benchmark's host price is not what we pay;
  * latencyMs null: the benchmark measured its own host's latency, not our provider's;
  * ok        true: the benchmark recorded an answer (a wrong answer is low quality, not a failed call).
A provider maps to a benchmark model only when it serves that exact model (for example Groq, Cerebras and the
HF router serve gpt-oss-120b, which xRouteBench measured). Providers without such a model get no rows; nothing
is guessed for them.

Task classes are Darwin-Route's (coding, research, reasoning, extraction, conversation). A benchmark task is
classed by documented rules; the manifest records which rule decided every task and --class-map overrides any.

Data rights: neither ulab-ai/xRouteBench nor withmartian/routerbench declares a license on its dataset card.
Immaculate's external-data rules default to DENY, so running this for real waits on the owner's license and
intended-use decision (recorded in Immaculate's CURRENT_HANDOFF.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

TASK_CLASSES = ("coding", "research", "reasoning", "extraction", "conversation")

# Benchmark sub-task -> Darwin-Route task class, first match wins on the lower-cased task name.
TASK_NAME_RULES: tuple[tuple[str, str], ...] = (
    ("mbpp", "coding"),
    ("humaneval", "coding"),
    ("code", "coding"),
    ("gsm8k", "reasoning"),
    ("math", "reasoning"),
    ("aime", "reasoning"),
    ("svamp", "reasoning"),
    ("bbh", "reasoning"),
    ("arc", "reasoning"),
    ("hellaswag", "reasoning"),
    ("winogrande", "reasoning"),
    ("piqa", "reasoning"),
    ("commonsense", "reasoning"),
    ("openbook", "reasoning"),
    ("boolq", "reasoning"),
    ("logiqa", "reasoning"),
    ("timeseries", "reasoning"),
    ("mmlu", "research"),
    ("gpqa", "research"),
    ("trivia", "research"),
    ("natural_q", "research"),
    ("nq", "research"),
    ("rag", "research"),
    ("squad", "extraction"),
    ("drop", "extraction"),
    ("quac", "extraction"),
    ("coqa", "extraction"),
    ("race", "extraction"),
    ("locomo", "extraction"),
    ("longmemeval", "extraction"),
    ("summar", "extraction"),
    ("translat", "extraction"),
    ("paraphras", "extraction"),
    ("mt-bench", "conversation"),
    ("mt_bench", "conversation"),
    ("alpaca", "conversation"),
    ("chat", "conversation"),
    ("personalized", "conversation"),
)
METRIC_RULES = {
    "code_eval": "coding",
    "gsm8k": "reasoning",
    "math": "reasoning",
    "em_mc": "reasoning",
    "mc": "reasoning",
    "em": "reasoning",
    "f1": "extraction",
    "llm_judge": "conversation",
}


class EvidenceError(ValueError):
    """Input that cannot become route observations."""


def classify_task(task_name: str, metric: str, overrides: dict[str, str] | None = None) -> tuple[str, str]:
    name = task_name.strip().lower()
    if overrides and name in overrides:
        task_class = overrides[name]
        if task_class not in TASK_CLASSES:
            raise EvidenceError(f"--class-map maps {task_name!r} to unknown task class {task_class!r}")
        return task_class, "class-map"
    for needle, task_class in TASK_NAME_RULES:
        if needle in name:
            return task_class, f"task-name:{needle}"
    by_metric = METRIC_RULES.get(metric.strip().lower())
    if by_metric:
        return by_metric, f"metric:{metric.strip().lower()}"
    raise EvidenceError(f"no task class for task {task_name!r} (metric {metric!r}); add it with --class-map")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_catalog(path: Path) -> list[dict]:
    """[{providerId, benchmarkModel, inputPer1M, outputPer1M}], the operator's own prices per provider."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, list) or not providers:
        raise EvidenceError("the catalog needs a non-empty 'providers' list")
    catalog, seen = [], set()
    for entry in providers:
        provider_id = str(entry.get("providerId", "")).strip()
        model = str(entry.get("benchmarkModel", "") or "").strip()
        if not provider_id or provider_id in seen:
            raise EvidenceError(f"catalog provider ids must be unique and non-empty (got {provider_id!r})")
        seen.add(provider_id)
        try:
            input_price, output_price = float(entry["inputPer1M"]), float(entry["outputPer1M"])
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceError(f"{provider_id}: inputPer1M and outputPer1M are required (0 for a free tier)") from error
        if input_price < 0 or output_price < 0:
            raise EvidenceError(f"{provider_id}: prices cannot be negative")
        catalog.append({"providerId": provider_id, "benchmarkModel": model or None, "inputPer1M": input_price, "outputPer1M": output_price})
    return catalog


def observation(task_class: str, provider: dict, performance: float, input_tokens: int | None, output_tokens: int | None, source: str) -> dict:
    cost = None
    if input_tokens is not None and output_tokens is not None:
        cost = input_tokens * provider["inputPer1M"] / 1e6 + output_tokens * provider["outputPer1M"] / 1e6
    return {
        "taskClass": task_class,
        "providerId": provider["providerId"],
        "ok": True,
        "latencyMs": None,
        "costUsd": cost,
        "quality": min(1.0, max(0.0, float(performance))),
        "source": source,
    }


class _Classifier:
    def __init__(self, overrides: dict[str, str]):
        self.overrides = overrides
        self.decided: dict[str, dict[str, str]] = {}

    def __call__(self, task_name: str, metric: str) -> str:
        key = task_name.strip().lower()
        if key not in self.decided:
            task_class, rule = classify_task(task_name, metric, self.overrides)
            self.decided[key] = {"task_class": task_class, "rule": rule}
        return self.decided[key]["task_class"]


def xroutebench_observations(records, catalog: list[dict], classify: _Classifier, source: str) -> list[dict]:
    """records: iterable of objects with task_name, metric, model_name, performance, input_tokens, output_tokens."""
    by_model: dict[str, list[dict]] = {}
    for provider in catalog:
        if provider["benchmarkModel"]:
            by_model.setdefault(provider["benchmarkModel"], []).append(provider)
    rows: list[dict] = []
    for record in records:
        providers = by_model.get(str(record.model_name))
        if not providers:
            continue
        task_class = classify(str(record.task_name), str(record.metric))
        for provider in providers:
            rows.append(observation(task_class, provider, record.performance, int(record.input_tokens), int(record.output_tokens), source))
    return rows


def routerbench_observations(records: list[dict], catalog: list[dict], classify: _Classifier, source: str) -> list[dict]:
    """RouterBench wide rows: '<model>' score columns; its costs are the host's, so tokens are unknown."""
    rows: list[dict] = []
    for record in records:
        task_class = classify(str(record["eval_name"]), "")
        for provider in catalog:
            model = provider["benchmarkModel"]
            if not model or model not in record:
                continue
            score = record[model]
            if score is None or score != score:  # NaN-safe
                continue
            rows.append(observation(task_class, provider, score, None, None, source))
    return rows


def _require_pandas():
    try:
        import pandas
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise SystemExit("This source needs pandas and pyarrow: pip install pandas pyarrow") from error
    return pandas


def write_output(out: Path, rows: list[dict], manifest: dict) -> dict:
    if not rows:
        raise EvidenceError("no catalog provider serves a model this benchmark measured; nothing to write")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    cells = Counter(f"{row['taskClass']}/{row['providerId']}" for row in rows)
    manifest.update(
        {
            "schema": "arobi.route-evidence.v1",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "observations": len(rows),
            "observations_sha256": sha256_file(out),
            "cells": dict(sorted(cells.items())),
        }
    )
    manifest_path = out.with_suffix(out.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _load_class_map(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(key).strip().lower(): str(value) for key, value in raw.items()}


def cmd_xroutebench(args) -> dict:
    pandas = _require_pandas()
    catalog = load_catalog(args.catalog)
    classify = _Classifier(_load_class_map(args.class_map))
    rows: list[dict] = []
    for path in args.rows:
        frame = pandas.read_parquet(path)
        rows.extend(xroutebench_observations(frame.itertuples(index=False), catalog, classify, f"xroutebench:{path.name}"))
    return write_output(
        args.out,
        rows,
        {
            "source": "ulab-ai/xRouteBench",
            "files": {str(path): sha256_file(path) for path in args.rows},
            "catalog_sha256": sha256_file(args.catalog),
            "task_classes": dict(sorted(classify.decided.items())),
            "latency": "null: the benchmark host's latency is not our provider's",
            "cost": "measured tokens x the catalog provider's own price",
        },
    )


def cmd_routerbench(args) -> dict:
    digest = sha256_file(args.pickle)
    if digest != args.expect_sha256.strip().lower():
        raise SystemExit(f"refusing to unpickle {args.pickle}: sha256 {digest} does not match --expect-sha256")
    pandas = _require_pandas()
    catalog = load_catalog(args.catalog)
    classify = _Classifier(_load_class_map(args.class_map))
    records = pandas.read_pickle(args.pickle).to_dict("records")
    return write_output(
        args.out,
        routerbench_observations(records, catalog, classify, f"routerbench:{args.pickle.name}"),
        {
            "source": "withmartian/routerbench",
            "files": {str(args.pickle): digest},
            "catalog_sha256": sha256_file(args.catalog),
            "task_classes": dict(sorted(classify.decided.items())),
            "latency": "null: not recorded for our providers",
            "cost": "null: RouterBench records its host's cost, not token counts",
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arobi_integrations.route_evidence", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="source", required=True)
    xr = sub.add_parser("xroutebench")
    xr.add_argument("--rows", type=Path, action="append", required=True, help="llmrouter_generic/{train,test}.parquet")
    rb = sub.add_parser("routerbench")
    rb.add_argument("--pickle", type=Path, required=True)
    rb.add_argument("--expect-sha256", required=True, help="the pickle's published sha256, checked before unpickling")
    for p in (xr, rb):
        p.add_argument("--catalog", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--class-map", type=Path, help="JSON object: benchmark task name -> Darwin-Route task class")
    xr.set_defaults(run=cmd_xroutebench)
    rb.set_defaults(run=cmd_routerbench)
    args = parser.parse_args(argv)
    try:
        manifest = args.run(args)
    except EvidenceError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(args.out), "observations": manifest["observations"], "cells": manifest["cells"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
