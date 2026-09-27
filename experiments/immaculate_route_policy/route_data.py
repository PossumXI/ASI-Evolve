"""Shared data model and fitness for the Immaculate route-policy experiment.

Every number this module works with is a measured outcome: one row per (query, candidate model) with the
task score the candidate earned on that query, the tokens it used and the price it would have cost. The
sources are public routing benchmarks (xRouteBench, RouterBench) and our own measurements of the providers
Immaculate actually routes to (score_responses.py). Nothing is sampled or invented; fitness is a replay of
those recorded outcomes.

The route table this experiment produces is keyed the way the Arobi spine already describes work: the
TaskClass and ComplexityBand that Asgard's classifier assigns (Websites/netlify/functions/_lib/
engineering-intelligence.ts). The complexity band below mirrors that classifier exactly, so an offline cell
and a production request land in the same place.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Iterator

TASK_CLASSES = ("retrieval", "extraction", "transform", "reasoning", "codegen", "verification", "conversation")
COMPLEXITY_BANDS = ("trivial", "low", "moderate", "high")
DEFAULT_CELL = "*|*"

# Mirrors Asgard's classifier: CODE_FENCE, estimateTokens and the ComplexityBand thresholds.
_CODE_FENCE = re.compile(r"```|\bfunction\b|\bclass\b|=>|\bimport\s|\bdef\s|;\s*$", re.M)


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def complexity_band(text: str) -> str:
    estimated = estimate_tokens(text)
    has_code = bool(_CODE_FENCE.search(text))
    lines = len(re.split(r"\r?\n", text)) if text else 0
    if estimated > 4000 or (has_code and lines > 60):
        return "high"
    if estimated > 1200 or (has_code and lines > 15):
        return "moderate"
    if estimated > 200:
        return "low"
    return "trivial"


# Benchmark sub-task -> TaskClass. Matched on the lower-cased task name, first rule wins. These are
# judgement calls about what kind of work each benchmark measures; prepare.py records which rule mapped
# every task name in the manifest, and --class-map overrides any of them.
_TASK_NAME_RULES: tuple[tuple[str, str], ...] = (
    ("mbpp", "codegen"),
    ("humaneval", "codegen"),
    ("code", "codegen"),
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
    ("mmlu", "retrieval"),
    ("gpqa", "retrieval"),
    ("trivia", "retrieval"),
    ("natural_q", "retrieval"),
    ("nq", "retrieval"),
    ("rag", "retrieval"),
    ("squad", "extraction"),
    ("drop", "extraction"),
    ("quac", "extraction"),
    ("coqa", "extraction"),
    ("race", "extraction"),
    ("locomo", "extraction"),
    ("longmemeval", "extraction"),
    ("summar", "transform"),
    ("translat", "transform"),
    ("paraphras", "transform"),
    ("rewrite", "transform"),
    ("mt-bench", "conversation"),
    ("mt_bench", "conversation"),
    ("alpaca", "conversation"),
    ("chat", "conversation"),
    ("personalized", "conversation"),
)
# When no task-name rule matches, the scoring metric says what kind of answer was graded.
_METRIC_RULES = {
    "code_eval": "codegen",
    "gsm8k": "reasoning",
    "math": "reasoning",
    "em_mc": "reasoning",
    "mc": "reasoning",
    "em": "reasoning",
    "f1": "extraction",
    "llm_judge": "conversation",
}


class DataError(ValueError):
    """Input data that cannot be used as measured outcomes."""


def classify_task(task_name: str, metric: str, overrides: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (task_class, rule) for a benchmark sub-task."""
    name = task_name.strip().lower()
    if overrides and name in overrides:
        task_class = overrides[name]
        if task_class not in TASK_CLASSES:
            raise DataError(f"--class-map maps {task_name!r} to unknown task class {task_class!r}")
        return task_class, "class-map"
    for needle, task_class in _TASK_NAME_RULES:
        if needle in name:
            return task_class, f"task-name:{needle}"
    by_metric = _METRIC_RULES.get(metric.strip().lower())
    if by_metric:
        return by_metric, f"metric:{metric.strip().lower()}"
    raise DataError(f"no task class for task {task_name!r} (metric {metric!r}); add it with --class-map")


@dataclass(frozen=True)
class Outcome:
    """One measured (query, candidate) outcome."""

    task_key: str  # "<task_name>:<task_id>", identical for every candidate on the same query
    task_name: str
    task_class: str
    complexity: str
    candidate: str
    performance: float  # task score in [0, 1]
    cost_usd: float
    input_tokens: int
    output_tokens: int
    latency_s: float | None

    @property
    def cell(self) -> str:
        return f"{self.task_class}|{self.complexity}"


def outcome_from_dict(raw: dict) -> Outcome:
    try:
        outcome = Outcome(
            task_key=str(raw["task_key"]),
            task_name=str(raw["task_name"]),
            task_class=str(raw["task_class"]),
            complexity=str(raw["complexity"]),
            candidate=str(raw["candidate"]),
            performance=float(raw["performance"]),
            cost_usd=float(raw["cost_usd"]),
            input_tokens=int(raw["input_tokens"]),
            output_tokens=int(raw["output_tokens"]),
            latency_s=None if raw.get("latency_s") is None else float(raw["latency_s"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DataError(f"malformed outcome row: {error}") from error
    if outcome.task_class not in TASK_CLASSES:
        raise DataError(f"unknown task class {outcome.task_class!r}")
    if outcome.complexity not in COMPLEXITY_BANDS:
        raise DataError(f"unknown complexity band {outcome.complexity!r}")
    if not 0.0 <= outcome.performance <= 1.0 or not math.isfinite(outcome.performance):
        raise DataError(f"performance {outcome.performance} outside [0, 1] for {outcome.task_key}")
    if outcome.cost_usd < 0 or not math.isfinite(outcome.cost_usd):
        raise DataError(f"negative or non-finite cost for {outcome.task_key}")
    return outcome


def read_outcomes(path: Path) -> list[Outcome]:
    rows: list[Outcome] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(outcome_from_dict(json.loads(line)))
            except (json.JSONDecodeError, DataError) as error:
                raise DataError(f"{path}:{line_number}: {error}") from error
    return rows


def write_outcomes(path: Path, rows: Iterable[Outcome]) -> int:
    count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")
            count += 1
    return count


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_bucket(task_key: str) -> float:
    """A stable position in [0, 1) for a query, so every candidate of a query lands in the same fold."""
    return int(hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:12], 16) / float(1 << 48)


def group_by_task(rows: Iterable[Outcome]) -> dict[str, dict[str, Outcome]]:
    tasks: dict[str, dict[str, Outcome]] = {}
    for row in rows:
        tasks.setdefault(row.task_key, {})[row.candidate] = row
    return tasks


def complete_tasks(rows: list[Outcome], candidates: Iterable[str]) -> dict[str, dict[str, Outcome]]:
    """Queries that every candidate answered. A replay only compares candidates on the same queries."""
    wanted = set(candidates)
    return {key: by_candidate for key, by_candidate in group_by_task(rows).items() if wanted <= set(by_candidate)}


# ── Statistics the policy program sees ────────────────────────────────────────────────────────────────


def _summary(rows: list[Outcome]) -> dict:
    latencies = [row.latency_s for row in rows if row.latency_s is not None]
    return {
        "n": len(rows),
        "performance": sum(row.performance for row in rows) / len(rows),
        "cost_usd": sum(row.cost_usd for row in rows) / len(rows),
        "latency_s": (sum(latencies) / len(latencies)) if latencies else None,
    }


def fit_statistics(rows: list[Outcome]) -> dict:
    """Per-candidate means over the fit fold: global, per task class, and per (class, band) cell."""
    buckets: dict[tuple[str, str, str], list[Outcome]] = {}
    for row in rows:
        buckets.setdefault(("global", "", row.candidate), []).append(row)
        buckets.setdefault(("by_class", row.task_class, row.candidate), []).append(row)
        buckets.setdefault(("by_cell", row.cell, row.candidate), []).append(row)
    stats: dict = {"global": {}, "by_class": {}, "by_cell": {}}
    for (scope, key, candidate), bucket in sorted(buckets.items()):
        summary = _summary(bucket)
        if scope == "global":
            stats["global"][candidate] = summary
        else:
            stats[scope].setdefault(key, {})[candidate] = summary
    return stats


def cost_reference(rows: list[Outcome]) -> float:
    """The median non-zero per-query cost in the fit fold: the unit λ is expressed in.

    When every outcome is free (all free-tier providers), the cost term is zero whatever the scale, and 1.0
    keeps the arithmetic defined.
    """
    positive = [row.cost_usd for row in rows if row.cost_usd > 0]
    return float(median(positive)) if positive else 1.0


def utility(outcome: Outcome, lambda_cost: float, cost_ref: float) -> float:
    return outcome.performance - lambda_cost * (outcome.cost_usd / cost_ref)


def utility_of_summary(summary: dict, params: dict) -> float:
    return summary["performance"] - params["lambda_cost"] * summary["cost_usd"] / params["cost_reference_usd"]


# ── Tables and replay ─────────────────────────────────────────────────────────────────────────────────


def normalize_order(order: object, candidates: list[str], fallback: list[str]) -> list[str]:
    """Validate a program's ranking and complete it with the fallback order for anything it left out."""
    if not isinstance(order, (list, tuple)) or not order:
        raise DataError("the policy must return a non-empty list of candidate names")
    known = set(candidates)
    seen: list[str] = []
    for name in order:
        if not isinstance(name, str) or name not in known:
            raise DataError(f"the policy returned an unknown candidate {name!r}")
        if name in seen:
            raise DataError(f"the policy returned {name!r} twice")
        seen.append(name)
    return seen + [name for name in fallback if name not in seen]


def replay(
    table: dict[str, list[str]],
    tasks: dict[str, dict[str, Outcome]],
    lambda_cost: float,
    cost_ref: float,
) -> dict:
    """Serve every query with the first candidate its cell's order names, and score what that earned."""
    if not tasks:
        raise DataError("no complete queries to replay")
    total_utility = total_performance = total_cost = second_performance = 0.0
    oracle_utility = 0.0
    served_counts: dict[str, int] = {}
    for by_candidate in tasks.values():
        sample = next(iter(by_candidate.values()))
        order = table.get(sample.cell) or table[DEFAULT_CELL]
        first = by_candidate[order[0]]
        second = by_candidate[order[1]] if len(order) > 1 else first
        total_utility += utility(first, lambda_cost, cost_ref)
        total_performance += first.performance
        total_cost += first.cost_usd
        second_performance += second.performance
        oracle_utility += max(utility(row, lambda_cost, cost_ref) for row in by_candidate.values())
        served_counts[first.candidate] = served_counts.get(first.candidate, 0) + 1
    count = len(tasks)
    return {
        "queries": count,
        "fitness": total_utility / count,
        "performance": total_performance / count,
        "cost_per_1k_usd": 1000.0 * total_cost / count,
        "second_choice_performance": second_performance / count,
        "oracle_fitness": oracle_utility / count,
        "served_share": {name: served / count for name, served in sorted(served_counts.items())},
    }


def best_single_candidate(tasks: dict[str, dict[str, Outcome]], candidates: list[str], lambda_cost: float, cost_ref: float) -> str:
    """The one candidate that would do best if it served everything (the baseline routers must beat)."""
    def mean_utility(name: str) -> float:
        return sum(utility(by_candidate[name], lambda_cost, cost_ref) for by_candidate in tasks.values()) / len(tasks)

    return max(sorted(candidates), key=mean_utility)


def cells_in(rows: Iterable[Outcome]) -> list[str]:
    return sorted({row.cell for row in rows})


def iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
