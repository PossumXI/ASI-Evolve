"""Score our own providers' answers on benchmark queries, producing canonical outcomes.

    python score_responses.py --queries queries.jsonl --responses responses.jsonl --prices prices.json \
        --out measured_outcomes.jsonl

queries.jsonl    from `prepare.py export-queries` (xRouteBench raw queries with ground truth).
responses.jsonl  from Immaculate's `npm run q:route-measure`, one line per (query, provider):
                 {task_key, providerId, model, response, usage: {prompt_tokens, completion_tokens} | null,
                  latencyMs, error?}
prices.json      the operator's actual per-1M-token prices per provider (a free tier is 0):
                 {"prices_per_1m_usd": {"router-groq": {"input": 0.15, "output": 0.6}, ...}}

Each outcome's candidate is the provider id. A provider error scores 0 (in production it would have fallen
through to the next provider), so reliability is part of the measurement. Metrics follow xRouteBench's
metric names: em_mc/mc (the chosen option letter), GSM8K (the final number), MATH (the final \\boxed{}
expression, normalized), f1 (token F1, best over references), em (normalized exact match). code_eval needs a
sandbox to execute code and llm_judge needs a judge model; both are skipped and counted, never guessed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import string
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_data as rd  # noqa: E402

UNSCORED_METRICS = {"code_eval", "llm_judge"}


# ── Normalization ─────────────────────────────────────────────────────────────────────────────────────


def normalize_answer(text: str) -> str:
    """SQuAD-style: lowercase, drop punctuation and articles, collapse whitespace."""
    lowered = text.lower()
    no_punct = "".join(char for char in lowered if char not in set(string.punctuation))
    no_articles = re.sub(r"\b(a|an|the)\b", " ", no_punct)
    return " ".join(no_articles.split())


def references(ground_truth: str) -> list[str]:
    """Ground truth may be a plain string or a JSON / Python list of acceptable answers."""
    text = (ground_truth or "").strip()
    if text.startswith("["):
        for parse in (json.loads, ast.literal_eval):
            try:
                value = parse(text)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]
    return [text] if text else []


def token_f1(prediction: str, reference: str) -> float:
    predicted = normalize_answer(prediction).split()
    expected = normalize_answer(reference).split()
    if not predicted or not expected:
        return float(predicted == expected)
    common = Counter(predicted) & Counter(expected)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def last_number(text: str) -> float | None:
    matches = _NUMBER.findall(text.replace("$", ""))
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", ""))
    except ValueError:
        return None


def last_boxed(text: str) -> str | None:
    """The content of the last \\boxed{...} (or \\fbox{...}), with balanced braces."""
    start = max(text.rfind("\\boxed{"), text.rfind("\\fbox{"))
    if start < 0:
        return None
    index = text.index("{", start) + 1
    depth = 1
    begin = index
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    return text[begin : index - 1] if depth == 0 else None


def normalize_math(expression: str) -> str:
    value = expression.strip().strip("$").strip()
    for old, new in (("\\left", ""), ("\\right", ""), ("\\!", ""), ("\\,", ""), ("\\dfrac", "\\frac"), ("\\tfrac", "\\frac")):
        value = value.replace(old, new)
    value = re.sub(r"\^\{?\\circ\}?", "", value)
    value = value.replace("\\%", "").replace("%", "")
    value = re.sub(r"\\text\{([^}]*)\}", r"\1", value)
    value = value.replace(" ", "").rstrip(".")
    return value


def chosen_letter(response: str, labels: list[str]) -> str | None:
    allowed = "".join(labels) or "ABCDEFGHIJ"
    patterns = (
        rf"answer\s*(?:is|:)?\s*\(?([{allowed}])\)?\b",
        rf"^\s*\(?([{allowed}])\)?[\s.:)]",
        rf"\(([{allowed}])\)",
        rf"\b([{allowed}])\b",
    )
    for pattern in patterns:
        match = re.search(pattern, response, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).upper()
    return None


def parse_choices(raw: str | None) -> tuple[list[str], list[str]]:
    if not raw:
        return [], []
    try:
        value = ast.literal_eval(raw) if raw.strip().startswith("{") else json.loads(raw)
    except (ValueError, SyntaxError):
        return [], []
    if isinstance(value, dict):
        return [str(item) for item in value.get("labels", [])], [str(item) for item in value.get("text", [])]
    return [], []


# ── Scoring ───────────────────────────────────────────────────────────────────────────────────────────


def score(metric: str, response: str, ground_truth: str, choices: str | None) -> float:
    name = metric.strip().lower()
    refs = references(ground_truth)
    if name in ("em_mc", "mc"):
        labels, texts = parse_choices(choices)
        expected = refs[0].strip() if refs else ""
        if expected.upper() not in labels and texts:
            normalized = normalize_answer(expected)
            for label, text in zip(labels, texts):
                if normalize_answer(text) == normalized:
                    expected = label
                    break
        predicted = chosen_letter(response, labels)
        return float(predicted is not None and predicted == expected.upper())
    if name == "gsm8k":
        expected = last_number(refs[0]) if refs else None
        predicted = last_number(response)
        return float(expected is not None and predicted is not None and abs(expected - predicted) < 1e-6)
    if name == "math":
        expected = last_boxed(refs[0]) if refs and last_boxed(refs[0]) is not None else (refs[0] if refs else "")
        predicted = last_boxed(response)
        if predicted is None:
            return 0.0
        left, right = normalize_math(predicted), normalize_math(expected)
        if left == right:
            return 1.0
        try:
            return float(abs(float(left) - float(right)) < 1e-6)
        except ValueError:
            return 0.0
    if name == "f1":
        return max((token_f1(response, ref) for ref in refs), default=0.0)
    if name == "em":
        predicted = normalize_answer(response)
        return float(any(predicted == normalize_answer(ref) for ref in refs))
    raise rd.DataError(f"no scorer for metric {metric!r}")


def score_responses(queries: dict[str, dict], responses: list[dict], prices: dict[str, dict]) -> tuple[list[rd.Outcome], dict]:
    classify_cache: dict[tuple[str, str], str] = {}
    outcomes: list[rd.Outcome] = []
    summary = Counter()
    for response in responses:
        task_key = str(response["task_key"])
        query = queries.get(task_key)
        if query is None:
            raise rd.DataError(f"response for unknown query {task_key}")
        provider = str(response["providerId"])
        if provider not in prices:
            raise rd.DataError(f"no price for provider {provider}; add it to prices.json (0 for a free tier)")
        metric = str(query["metric"])
        if metric.strip().lower() in UNSCORED_METRICS:
            summary[f"skipped:{metric}"] += 1
            continue
        key = (query["task_name"], metric)
        if key not in classify_cache:
            classify_cache[key] = rd.classify_task(query["task_name"], metric)[0]
        text = str(response.get("response") or "")
        failed = bool(response.get("error")) or not text.strip()
        usage = response.get("usage") or {}
        if failed:
            input_tokens = output_tokens = 0
            summary["provider_errors"] += 1
        elif usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None:
            input_tokens, output_tokens = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        else:
            input_tokens, output_tokens = rd.estimate_tokens(query["query"]), rd.estimate_tokens(text)
            summary["tokens_estimated"] += 1
        price = prices[provider]
        latency_ms = response.get("latencyMs")
        outcomes.append(
            rd.Outcome(
                task_key=task_key,
                task_name=str(query["task_name"]),
                task_class=classify_cache[key],
                complexity=rd.complexity_band(str(query["query"])),
                candidate=provider,
                performance=0.0 if failed else score(metric, text, str(query["ground_truth"]), query.get("choices")),
                cost_usd=input_tokens * float(price["input"]) / 1e6 + output_tokens * float(price["output"]) / 1e6,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=None if latency_ms is None else float(latency_ms) / 1000.0,
            )
        )
        summary["scored"] += 1
    return outcomes, dict(summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    queries = {str(row["task_key"]): row for row in rd.iter_jsonl(args.queries)}
    prices = json.loads(args.prices.read_text(encoding="utf-8")).get("prices_per_1m_usd", {})
    outcomes, summary = score_responses(queries, list(rd.iter_jsonl(args.responses)), prices)
    rd.write_outcomes(args.out, outcomes)
    print(json.dumps({"out": str(args.out), **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
