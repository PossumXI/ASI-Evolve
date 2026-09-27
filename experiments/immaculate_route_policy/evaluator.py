"""Fitness of a route-policy program, by replaying measured outcomes.

    python evaluator.py CODE_PATH RESULTS_PATH [--data-dir DIR] [--lambda-cost 0.5] [--timeout-secs 120]

The program's rank_candidates() is called once per cell with statistics from the FIT fold. The resulting
table serves every query of the SEARCH fold with the first candidate its cell names, and fitness is the mean
of performance - lambda_cost * cost / cost_reference over those queries. The HOLDOUT fold is never read
here: compile_table.py scores it once, when a table is proposed for production.

results.json carries `fitness` (the run spec's core score, mirrored as `eval_score` and `score`), the
baselines a router must beat (the best single candidate and the oracle), and secondary metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_data as rd  # noqa: E402

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
FAILED_FITNESS = -1.0e9


class EvaluationTimeout(Exception):
    pass


def _on_alarm(signum, frame):  # pragma: no cover - exercised only on a real timeout
    raise EvaluationTimeout("evaluation exceeded its timeout")


def load_program(code_path: Path):
    # The pipeline stores candidates as `code` (no .py suffix), so the loader is named explicitly.
    loader = importlib.machinery.SourceFileLoader("route_policy_under_evaluation", str(code_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise rd.DataError(f"cannot load program from {code_path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    if not callable(getattr(module, "rank_candidates", None)):
        raise rd.DataError("the program must define rank_candidates(cell, stats, params)")
    return module


def build_table(program, fit_rows: list[rd.Outcome], cells: list[str], candidates: list[str], params: dict) -> dict[str, list[str]]:
    stats = rd.fit_statistics(fit_rows)
    global_order = sorted(
        candidates,
        key=lambda name: (-rd.utility_of_summary(stats["global"][name], params), name),
    )
    table: dict[str, list[str]] = {}
    for cell in [*cells, rd.DEFAULT_CELL]:
        task_class, complexity = cell.split("|", 1)
        ranked = program.rank_candidates({"task_class": task_class, "complexity": complexity}, stats, dict(params))
        table[cell] = rd.normalize_order(ranked, candidates, global_order)
    return table


def table_digest(table: dict[str, list[str]]) -> str:
    return hashlib.sha256(json.dumps(table, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def evaluate(code_path: Path, data_dir: Path, lambda_cost: float) -> dict:
    started = time.monotonic()
    fit_rows = rd.read_outcomes(data_dir / "fit.jsonl")
    search_rows = rd.read_outcomes(data_dir / "search.jsonl")
    candidates = sorted({row.candidate for row in fit_rows})
    if len(candidates) < 2:
        raise rd.DataError("routing needs at least two candidates in the fit fold")
    fit_tasks = rd.complete_tasks(fit_rows, candidates)
    search_tasks = rd.complete_tasks(search_rows, candidates)
    cost_ref = rd.cost_reference(fit_rows)
    params = {"lambda_cost": lambda_cost, "cost_reference_usd": cost_ref}

    program = load_program(code_path)
    cells = rd.cells_in([*fit_rows, *search_rows])
    table = build_table(program, fit_rows, cells, candidates, params)
    replayed = rd.replay(table, search_tasks, lambda_cost, cost_ref)

    best_single = rd.best_single_candidate(fit_tasks, candidates, lambda_cost, cost_ref)
    single_table = {rd.DEFAULT_CELL: [best_single] + [name for name in candidates if name != best_single]}
    single = rd.replay(single_table, search_tasks, lambda_cost, cost_ref)

    fitness = replayed["fitness"]
    return {
        "success": True,
        "fitness": fitness,
        "eval_score": fitness,
        "score": fitness,
        "performance": replayed["performance"],
        "cost_per_1k_usd": replayed["cost_per_1k_usd"],
        "second_choice_performance": replayed["second_choice_performance"],
        "oracle_fitness": replayed["oracle_fitness"],
        "oracle_gap": replayed["oracle_fitness"] - fitness,
        "best_single": best_single,
        "best_single_fitness": single["fitness"],
        "uplift_vs_best_single": fitness - single["fitness"],
        "served_share": replayed["served_share"],
        "queries": replayed["queries"],
        "cells": len(cells),
        "candidates": candidates,
        "lambda_cost": lambda_cost,
        "cost_reference_usd": cost_ref,
        "table_digest": table_digest(table),
        "eval_time": time.monotonic() - started,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code_path", type=Path)
    parser.add_argument("results_path", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ROUTE_POLICY_DATA_DIR", DEFAULT_DATA_DIR)))
    parser.add_argument("--lambda-cost", type=float, default=float(os.environ.get("ROUTE_POLICY_LAMBDA_COST", "0.5")))
    parser.add_argument("--timeout-secs", type=int, default=int(os.environ.get("ROUTE_POLICY_TIMEOUT_SECS", "120")))
    args = parser.parse_args(argv)

    if args.timeout_secs > 0 and hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(args.timeout_secs)
    try:
        results = evaluate(args.code_path, args.data_dir, args.lambda_cost)
        exit_code = 0
    except Exception as error:  # the evolution loop records a failed candidate instead of crashing
        # Fitness can be negative for a valid, cost-heavy table, so a failure scores far below any of them.
        results = {
            "success": False,
            "fitness": FAILED_FITNESS,
            "eval_score": FAILED_FITNESS,
            "score": FAILED_FITNESS,
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(limit=5),
        }
        exit_code = 1
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    args.results_path.parent.mkdir(parents=True, exist_ok=True)
    args.results_path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
