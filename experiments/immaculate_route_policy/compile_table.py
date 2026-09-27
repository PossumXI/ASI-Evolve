"""Compile an evolved route policy into the table the Immaculate gateway loads.

    python compile_table.py BEST_PROGRAM --catalog provider_catalog.json --out route-order-table.json \
        [--data-dir data] [--lambda-cost 0.5] [--run-id RUN] [--allow-regression]

1. Statistics come from fit + search; the program ranks every cell.
2. The table is scored once on the HOLDOUT fold, which evolution never saw, against the best single
   candidate. A table that does worse than the best single candidate on the holdout is refused unless
   --allow-regression is given (and the manifest says so).
3. Candidates are mapped to Immaculate provider ids through the operator's catalog. A catalog entry maps a
   provider to the candidate whose outcomes describe it: the same model, measured (for our own providers,
   the candidate IS the provider id; see score_responses.py). Providers the data does not cover are listed
   as unmeasured and keep their configured order after the measured ones; nothing is guessed for them.

The output is `immaculate.route-order-table.v1`. The gateway loads it only through
IMMACULATE_ROUTE_ORDER_TABLE_PATH together with IMMACULATE_ROUTE_ORDER_TABLE_SHA256, and only reorders
fallbacks it already has configured; Q stays first and every gate still applies.

provider_catalog.json:
    {"providers": [{"providerId": "router-groq", "candidate": "gpt-oss-120b"},
                   {"providerId": "router-cloudflare", "candidate": null}]}
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluator  # noqa: E402
import route_data as rd  # noqa: E402

TABLE_SCHEMA = "immaculate.route-order-table.v1"


def load_catalog(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, list) or not providers:
        raise rd.DataError("the catalog needs a non-empty 'providers' list")
    seen: set[str] = set()
    catalog = []
    for entry in providers:
        provider_id = str(entry.get("providerId", "")).strip()
        if not provider_id or provider_id in seen:
            raise rd.DataError(f"catalog provider ids must be unique and non-empty (got {provider_id!r})")
        seen.add(provider_id)
        candidate = entry.get("candidate")
        catalog.append({"providerId": provider_id, "candidate": None if candidate in (None, "") else str(candidate)})
    return catalog


def provider_order(candidate_order: list[str], catalog: list[dict]) -> list[str]:
    """Measured providers in the candidate order (catalog order breaks ties between providers of one candidate)."""
    rank = {name: index for index, name in enumerate(candidate_order)}
    measured = [entry for entry in catalog if entry["candidate"] in rank]
    measured.sort(key=lambda entry: rank[entry["candidate"]])
    return [entry["providerId"] for entry in measured]


def compile_table(
    program_path: Path,
    data_dir: Path,
    catalog: list[dict],
    lambda_cost: float,
    run_id: str | None,
    allow_regression: bool,
) -> dict:
    fit_rows = rd.read_outcomes(data_dir / "fit.jsonl") + rd.read_outcomes(data_dir / "search.jsonl")
    holdout_rows = rd.read_outcomes(data_dir / "holdout.jsonl")
    candidates = sorted({row.candidate for row in fit_rows})
    covered = {entry["candidate"] for entry in catalog if entry["candidate"]}
    unknown = sorted(covered - set(candidates))
    if unknown:
        raise rd.DataError(f"catalog maps providers to candidate(s) with no outcomes: {', '.join(unknown)}")
    if len(covered) < 1:
        raise rd.DataError("no catalog provider maps to a measured candidate")

    cost_ref = rd.cost_reference(fit_rows)
    params = {"lambda_cost": lambda_cost, "cost_reference_usd": cost_ref}
    program = evaluator.load_program(program_path)
    cells = rd.cells_in(fit_rows)
    table = evaluator.build_table(program, fit_rows, cells, candidates, params)

    holdout_tasks = rd.complete_tasks(holdout_rows, candidates)
    fit_tasks = rd.complete_tasks(fit_rows, candidates)
    holdout = rd.replay(table, holdout_tasks, lambda_cost, cost_ref)
    best_single = rd.best_single_candidate(fit_tasks, candidates, lambda_cost, cost_ref)
    single = rd.replay(
        {rd.DEFAULT_CELL: [best_single] + [name for name in candidates if name != best_single]},
        holdout_tasks,
        lambda_cost,
        cost_ref,
    )
    regression = holdout["fitness"] < single["fitness"]
    if regression and not allow_regression:
        raise rd.DataError(
            f"holdout fitness {holdout['fitness']:.6f} is below the best single candidate "
            f"({best_single}: {single['fitness']:.6f}); refusing to compile (use --allow-regression to override)"
        )

    rows = []
    for cell, order in sorted(table.items()):
        providers = provider_order(order, catalog)
        if not providers:
            continue
        task_class, complexity = cell.split("|", 1)
        rows.append({"taskClass": task_class, "complexity": complexity, "order": providers})

    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return {
        "schema": TABLE_SCHEMA,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "kind": "asi-evolve",
            "runId": run_id,
            "programSha256": hashlib.sha256(program_path.read_bytes()).hexdigest(),
            "data": {
                "source": manifest.get("source"),
                "dataset": manifest.get("dataset"),
                "files": manifest.get("files", {}),
            },
        },
        "fitness": {
            "lambdaCost": lambda_cost,
            "costReferenceUsd": cost_ref,
            "holdout": holdout["fitness"],
            "holdoutPerformance": holdout["performance"],
            "holdoutCostPer1kUsd": holdout["cost_per_1k_usd"],
            "holdoutQueries": holdout["queries"],
            "bestSingle": best_single,
            "bestSingleHoldout": single["fitness"],
            "oracleHoldout": holdout["oracle_fitness"],
            "regressionAllowed": bool(regression and allow_regression),
        },
        "unmeasured": [entry["providerId"] for entry in catalog if entry["candidate"] is None],
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("program", type=Path)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=evaluator.DEFAULT_DATA_DIR)
    parser.add_argument("--lambda-cost", type=float, default=0.5)
    parser.add_argument("--run-id")
    parser.add_argument("--allow-regression", action="store_true")
    args = parser.parse_args(argv)
    try:
        table = compile_table(args.program, args.data_dir, load_catalog(args.catalog), args.lambda_cost, args.run_id, args.allow_regression)
    except rd.DataError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    encoded = (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    print(json.dumps({"out": str(args.out), "sha256": digest, "rows": len(table["rows"]), "fitness": table["fitness"]}, indent=2))
    print(f"\nIMMACULATE_ROUTE_ORDER_TABLE_PATH={args.out.resolve()}\nIMMACULATE_ROUTE_ORDER_TABLE_SHA256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
