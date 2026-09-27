"""Turn measured routing data into the canonical outcome folds the evaluator replays.

    python prepare.py xroutebench --train llmrouter_generic/train.parquet --test llmrouter_generic/test.parquet \
        --candidates llm_candidates/train.parquet [--out data] [--class-map map.json] [--search-fraction 0.2]
    python prepare.py routerbench --pickle routerbench_0shot.pkl --expect-sha256 <hex> [--out data]
    python prepare.py measured --outcomes measured_outcomes.jsonl [--out data]
    python prepare.py export-queries --queries llmrouter_generic_queries/train.parquet --per-task 40 --out queries.jsonl

Folds (all split by query, so every candidate of a query lands in the same fold):
  fit.jsonl       statistics the policy program sees
  search.jsonl    what the evolution loop scores
  holdout.jsonl   scored once by compile_table.py when a table is proposed; never read during evolution
xRouteBench's own train/test split is kept: its test split is the holdout. The other sources are split by a
stable hash of the query key. manifest.json records the source files' sha256, row counts, candidate prices,
and which rule gave every benchmark task its TaskClass.

Sources:
  xRouteBench   ulab-ai/xRouteBench (arXiv:2608.06867): 18 candidates, measured performance, tokens and
                latency per (query, model); cost = tokens x the published per-1M prices.
  RouterBench   withmartian/routerbench (arXiv:2403.12031): 11 candidates with per-query score and cost.
                The pickle has no token counts, so tokens are estimated as chars/4 (flagged in the manifest).
                Unpickling executes code: the file's sha256 must match --expect-sha256 before it is opened.
  measured      our own providers, scored by score_responses.py on the same queries.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_data as rd  # noqa: E402


def _require_pandas():
    try:
        import pandas  # noqa: F401
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise SystemExit("This source needs pandas and pyarrow: pip install pandas pyarrow") from error
    import pandas

    return pandas


def _load_class_map(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(key).strip().lower(): str(value) for key, value in raw.items()}


class _Classifier:
    """Maps task names to TaskClasses once each and remembers which rule decided."""

    def __init__(self, overrides: dict[str, str]):
        self.overrides = overrides
        self.decided: dict[str, dict[str, str]] = {}

    def __call__(self, task_name: str, metric: str) -> str:
        key = task_name.strip().lower()
        if key not in self.decided:
            task_class, rule = rd.classify_task(task_name, metric, self.overrides)
            self.decided[key] = {"task_class": task_class, "rule": rule}
        return self.decided[key]["task_class"]


def _write_folds(out: Path, folds: dict[str, list[rd.Outcome]], manifest: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, rows in folds.items():
        counts[name] = {
            "rows": rd.write_outcomes(out / f"{name}.jsonl", rows),
            "queries": len({row.task_key for row in rows}),
        }
    all_rows = [row for rows in folds.values() for row in rows]
    manifest.update(
        {
            "schema": "immaculate.route-outcomes.v1",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "folds": counts,
            "candidates": sorted({row.candidate for row in all_rows}),
            "cells": dict(sorted(Counter(row.cell for row in all_rows if row.candidate == all_rows[0].candidate).items())),
        }
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _hash_folds(rows: list[rd.Outcome], search_fraction: float, holdout_fraction: float) -> dict[str, list[rd.Outcome]]:
    folds: dict[str, list[rd.Outcome]] = {"fit": [], "search": [], "holdout": []}
    fit_cut = 1.0 - search_fraction - holdout_fraction
    for row in rows:
        position = rd.split_bucket(row.task_key)
        fold = "fit" if position < fit_cut else "search" if position < fit_cut + search_fraction else "holdout"
        folds[fold].append(row)
    return folds


# ── xRouteBench ───────────────────────────────────────────────────────────────────────────────────────


def xroutebench_rows(frame, prices: dict[str, tuple[float, float]], classify: _Classifier) -> list[rd.Outcome]:
    missing = sorted(set(frame["model_name"]) - set(prices))
    if missing:
        raise rd.DataError(f"no published price for candidate(s): {', '.join(missing)}")
    rows: list[rd.Outcome] = []
    for record in frame.itertuples(index=False):
        input_price, output_price = prices[record.model_name]
        input_tokens = int(record.input_tokens)
        output_tokens = int(record.output_tokens)
        query = str(record.query)
        rows.append(
            rd.Outcome(
                task_key=f"{record.task_name}:{record.task_id}",
                task_name=str(record.task_name),
                task_class=classify(str(record.task_name), str(record.metric)),
                complexity=rd.complexity_band(query),
                candidate=str(record.model_name),
                performance=min(1.0, max(0.0, float(record.performance))),
                cost_usd=input_tokens * input_price / 1e6 + output_tokens * output_price / 1e6,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=None if record.response_time is None else float(record.response_time),
            )
        )
    return rows


def cmd_xroutebench(args) -> dict:
    pandas = _require_pandas()
    candidates = pandas.read_parquet(args.candidates)
    prices = {
        str(row.model_name): (float(row.input_price_per_1m), float(row.output_price_per_1m))
        for row in candidates.itertuples(index=False)
    }
    classify = _Classifier(_load_class_map(args.class_map))
    train = xroutebench_rows(pandas.read_parquet(args.train), prices, classify)
    test = xroutebench_rows(pandas.read_parquet(args.test), prices, classify)
    folds = _hash_folds(train, args.search_fraction, 0.0)
    folds["holdout"] = test
    return _write_folds(
        args.out,
        folds,
        {
            "source": "xroutebench",
            "dataset": "ulab-ai/xRouteBench",
            "files": {str(path): rd.sha256_file(path) for path in (args.train, args.test, args.candidates)},
            "prices_per_1m_usd": {name: {"input": pair[0], "output": pair[1]} for name, pair in sorted(prices.items())},
            "tokens": "measured",
            "split": {"fit_search": "sha256(task_key)", "search_fraction": args.search_fraction, "holdout": "official test split"},
            "task_classes": dict(sorted(classify.decided.items())),
        },
    )


# ── RouterBench ───────────────────────────────────────────────────────────────────────────────────────


def routerbench_rows(frame, classify: _Classifier) -> tuple[list[rd.Outcome], list[str]]:
    models = [column for column in frame.columns if f"{column}|total_cost" in frame.columns]
    if len(models) < 2:
        raise rd.DataError("the RouterBench frame has fewer than two '<model>' / '<model>|total_cost' column pairs")
    rows: list[rd.Outcome] = []
    for record in frame.to_dict("records"):
        prompt = str(record["prompt"])
        eval_name = str(record["eval_name"])
        task_class = classify(eval_name, "")
        band = rd.complexity_band(prompt)
        for model in models:
            score = record[model]
            cost = record[f"{model}|total_cost"]
            if score is None or cost is None or score != score or cost != cost:  # NaN-safe
                continue
            response = str(record.get(f"{model}|model_response", "") or "")
            rows.append(
                rd.Outcome(
                    task_key=f"{eval_name}:{record['sample_id']}",
                    task_name=eval_name,
                    task_class=task_class,
                    complexity=band,
                    candidate=str(model),
                    performance=min(1.0, max(0.0, float(score))),
                    cost_usd=float(cost),
                    input_tokens=rd.estimate_tokens(prompt),
                    output_tokens=rd.estimate_tokens(response) if response else 0,
                    latency_s=None,
                )
            )
    return rows, models


def cmd_routerbench(args) -> dict:
    digest = rd.sha256_file(args.pickle)
    if digest != args.expect_sha256.strip().lower():
        raise SystemExit(f"refusing to unpickle {args.pickle}: sha256 {digest} does not match --expect-sha256")
    pandas = _require_pandas()
    frame = pandas.read_pickle(args.pickle)
    classify = _Classifier(_load_class_map(args.class_map))
    rows, models = routerbench_rows(frame, classify)
    folds = _hash_folds(rows, args.search_fraction, args.holdout_fraction)
    return _write_folds(
        args.out,
        folds,
        {
            "source": "routerbench",
            "dataset": "withmartian/routerbench",
            "files": {str(args.pickle): digest},
            "models": models,
            "tokens": "estimated (chars/4); RouterBench records cost, not token counts",
            "split": {"method": "sha256(task_key)", "search_fraction": args.search_fraction, "holdout_fraction": args.holdout_fraction},
            "task_classes": dict(sorted(classify.decided.items())),
        },
    )


# ── Our own measurements ──────────────────────────────────────────────────────────────────────────────


def cmd_measured(args) -> dict:
    rows = rd.read_outcomes(args.outcomes)
    folds = _hash_folds(rows, args.search_fraction, args.holdout_fraction)
    return _write_folds(
        args.out,
        folds,
        {
            "source": "measured",
            "files": {str(args.outcomes): rd.sha256_file(args.outcomes)},
            "tokens": "as reported by each provider (estimated where it reported none; see score_responses.py)",
            "split": {"method": "sha256(task_key)", "search_fraction": args.search_fraction, "holdout_fraction": args.holdout_fraction},
        },
    )


def cmd_export_queries(args) -> dict:
    """A deterministic, per-task sample of raw queries for measuring our own providers."""
    pandas = _require_pandas()
    frame = pandas.read_parquet(args.queries)
    by_task: dict[str, list[dict]] = {}
    for record in frame.to_dict("records"):
        key = f"{record['task_name']}:{record['task_id']}"
        by_task.setdefault(str(record["task_name"]), []).append({**record, "task_key": key})
    selected: list[dict] = []
    for task_name in sorted(by_task):
        ranked = sorted(by_task[task_name], key=lambda item: rd.split_bucket(item["task_key"]))
        selected.extend(ranked[: args.per_task])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for record in selected:
            handle.write(
                json.dumps(
                    {
                        "task_key": record["task_key"],
                        "task_name": str(record["task_name"]),
                        "task_id": str(record["task_id"]),
                        "metric": str(record["metric"]),
                        "query": str(record["query"]),
                        "ground_truth": str(record["ground_truth"]),
                        "choices": None if record.get("choices") is None else str(record["choices"]),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    summary = {"queries": len(selected), "tasks": len(by_task), "source": str(args.queries), "sha256": rd.sha256_file(args.queries)}
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="source", required=True)

    def common(p, holdout: bool):
        p.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "data")
        p.add_argument("--class-map", type=Path, help="JSON object: benchmark task name -> TaskClass")
        p.add_argument("--search-fraction", type=float, default=0.2)
        if holdout:
            p.add_argument("--holdout-fraction", type=float, default=0.2)

    xr = sub.add_parser("xroutebench")
    xr.add_argument("--train", type=Path, required=True)
    xr.add_argument("--test", type=Path, required=True)
    xr.add_argument("--candidates", type=Path, required=True)
    common(xr, holdout=False)
    xr.set_defaults(run=cmd_xroutebench)

    rb = sub.add_parser("routerbench")
    rb.add_argument("--pickle", type=Path, required=True)
    rb.add_argument("--expect-sha256", required=True, help="sha256 of the pickle as published; checked before unpickling")
    common(rb, holdout=True)
    rb.set_defaults(run=cmd_routerbench)

    ms = sub.add_parser("measured")
    ms.add_argument("--outcomes", type=Path, required=True)
    common(ms, holdout=True)
    ms.set_defaults(run=cmd_measured)

    eq = sub.add_parser("export-queries")
    eq.add_argument("--queries", type=Path, required=True)
    eq.add_argument("--per-task", type=int, default=40)
    eq.add_argument("--out", type=Path, required=True)
    eq.set_defaults(run=cmd_export_queries)

    args = parser.parse_args(argv)
    for name in ("search_fraction", "holdout_fraction"):
        value = getattr(args, name, 0.0)
        if not 0.0 <= value < 1.0:
            parser.error(f"--{name.replace('_', '-')} must be in [0, 1)")
    if getattr(args, "search_fraction", 0.0) + getattr(args, "holdout_fraction", 0.0) >= 1.0:
        parser.error("the fit fold would be empty")
    result = args.run(args)
    if args.source != "export-queries":
        print(json.dumps({"out": str(args.out), "folds": result["folds"], "candidates": result["candidates"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
