import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

EXPERIMENT = Path(__file__).resolve().parents[1] / "experiments" / "immaculate_route_policy"
sys.path.insert(0, str(EXPERIMENT))

import compile_table  # noqa: E402
import evaluator  # noqa: E402
import prepare  # noqa: E402
import route_data as rd  # noqa: E402
import score_responses  # noqa: E402

INITIAL_PROGRAM = EXPERIMENT / "initial_program"


def outcome(task_key, candidate, performance, cost, task_class="reasoning", complexity="trivial"):
    return rd.Outcome(
        task_key=task_key,
        task_name=task_key.split(":")[0],
        task_class=task_class,
        complexity=complexity,
        candidate=candidate,
        performance=performance,
        cost_usd=cost,
        input_tokens=100,
        output_tokens=50,
        latency_s=1.0,
    )


def specialist_world(queries_per_class=60):
    """Two task classes where a different specialist wins each, and a generalist is the best single candidate.

    The route table should learn to send codegen to `coder` and reasoning to `thinker`, beating the best
    single candidate. These rows are test fixtures describing a scenario, not measurements.
    """
    rows = []
    for index in range(queries_per_class):
        code_key = f"mbpp:{index}"
        rows += [
            outcome(code_key, "coder", 1.0, 0.0001, "codegen"),
            outcome(code_key, "thinker", 0.0, 0.0001, "codegen"),
            outcome(code_key, "generalist", 0.8 if index % 5 else 0.0, 0.0001, "codegen"),
        ]
        math_key = f"gsm8k:{index}"
        rows += [
            outcome(math_key, "coder", 0.0, 0.0001, "reasoning"),
            outcome(math_key, "thinker", 1.0, 0.0001, "reasoning"),
            outcome(math_key, "generalist", 0.8 if index % 5 else 0.0, 0.0001, "reasoning"),
        ]
    return rows


def write_folds(directory: Path, rows):
    tasks = sorted({row.task_key for row in rows})
    fit_keys = set(tasks[0::3]) | set(tasks[1::3])
    search_keys = set(tasks[2::6])
    folds = {"fit": [], "search": [], "holdout": []}
    for row in rows:
        if row.task_key in fit_keys:
            folds["fit"].append(row)
        elif row.task_key in search_keys:
            folds["search"].append(row)
        else:
            folds["holdout"].append(row)
    for name, fold_rows in folds.items():
        rd.write_outcomes(directory / f"{name}.jsonl", fold_rows)
    (directory / "manifest.json").write_text(json.dumps({"source": "test", "files": {}}), encoding="utf-8")


class RouteDataTests(unittest.TestCase):
    def test_complexity_band_mirrors_the_spine_classifier(self):
        self.assertEqual(rd.complexity_band("short question?"), "trivial")
        self.assertEqual(rd.complexity_band("x" * 900), "low")
        self.assertEqual(rd.complexity_band("x" * 5000), "moderate")
        self.assertEqual(rd.complexity_band("x" * 17000), "high")
        code = "\n".join(["def f(x):"] + ["    return x"] * 20)
        self.assertEqual(rd.complexity_band(code), "moderate")
        self.assertEqual(rd.complexity_band("\n".join(["def f(x):"] + ["    x += 1"] * 70)), "high")

    def test_task_classification_rules_metric_fallback_and_overrides(self):
        self.assertEqual(rd.classify_task("mbpp", "code_eval"), ("codegen", "task-name:mbpp"))
        self.assertEqual(rd.classify_task("gsm8k", "GSM8K")[0], "reasoning")
        self.assertEqual(rd.classify_task("mmlu_high_school_physics", "em_mc")[0], "retrieval")
        self.assertEqual(rd.classify_task("squad", "f1")[0], "extraction")
        self.assertEqual(rd.classify_task("unfamiliar_bench", "f1"), ("extraction", "metric:f1"))
        self.assertEqual(rd.classify_task("mmlu", "em_mc", {"mmlu": "reasoning"}), ("reasoning", "class-map"))
        with self.assertRaises(rd.DataError):
            rd.classify_task("unfamiliar_bench", "bleu")
        with self.assertRaises(rd.DataError):
            rd.classify_task("mmlu", "em_mc", {"mmlu": "astrology"})

    def test_split_is_stable_and_per_query(self):
        self.assertEqual(rd.split_bucket("gsm8k:1"), rd.split_bucket("gsm8k:1"))
        self.assertNotEqual(rd.split_bucket("gsm8k:1"), rd.split_bucket("gsm8k:2"))
        self.assertTrue(0.0 <= rd.split_bucket("anything") < 1.0)

    def test_outcome_rows_are_validated(self):
        good = {
            "task_key": "a:1", "task_name": "a", "task_class": "reasoning", "complexity": "low", "candidate": "m",
            "performance": 0.5, "cost_usd": 0.001, "input_tokens": 1, "output_tokens": 1, "latency_s": None,
        }
        self.assertEqual(rd.outcome_from_dict(good).candidate, "m")
        for field, value in (("performance", 1.5), ("cost_usd", -1), ("task_class", "poetry"), ("complexity", "huge")):
            with self.assertRaises(rd.DataError):
                rd.outcome_from_dict({**good, field: value})

    def test_all_free_outcomes_keep_the_cost_term_defined(self):
        self.assertEqual(rd.cost_reference([outcome("a:1", "m", 1.0, 0.0)]), 1.0)


class EvaluatorTests(unittest.TestCase):
    def test_the_baseline_program_learns_the_specialists_and_beats_the_best_single_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            write_folds(data, specialist_world())
            results = evaluator.evaluate(INITIAL_PROGRAM, data, lambda_cost=0.5)
        self.assertTrue(results["success"])
        self.assertEqual(results["candidates"], ["coder", "generalist", "thinker"])
        self.assertEqual(results["best_single"], "generalist")
        self.assertGreater(results["uplift_vs_best_single"], 0.0)
        self.assertAlmostEqual(results["performance"], 1.0)
        self.assertAlmostEqual(results["oracle_gap"], 0.0, places=9)
        self.assertEqual(results["fitness"], results["eval_score"])
        self.assertRegex(results["table_digest"], r"^[0-9a-f]{64}$")

    def test_a_broken_program_fails_with_a_floor_fitness_and_still_writes_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            write_folds(data, specialist_world())
            broken = Path(tmp) / "broken.py"
            broken.write_text("def rank_candidates(cell, stats, params):\n    return ['nobody']\n", encoding="utf-8")
            results_path = Path(tmp) / "results.json"
            code = evaluator.main([str(broken), str(results_path), "--data-dir", str(data), "--timeout-secs", "30"])
            results = json.loads(results_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertFalse(results["success"])
        self.assertEqual(results["fitness"], evaluator.FAILED_FITNESS)
        self.assertIn("unknown candidate", results["error"])

    def test_orders_are_validated_and_completed(self):
        self.assertEqual(rd.normalize_order(["b"], ["a", "b", "c"], ["c", "a", "b"]), ["b", "c", "a"])
        for bad in ([], ["b", "b"], ["z"], "b"):
            with self.assertRaises(rd.DataError):
                rd.normalize_order(bad, ["a", "b", "c"], ["a", "b", "c"])


class CompileTableTests(unittest.TestCase):
    def test_compiles_a_pinned_table_of_provider_orders_and_lists_unmeasured_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            write_folds(data, specialist_world())
            catalog = [
                {"providerId": "router-coder", "candidate": "coder"},
                {"providerId": "router-thinker", "candidate": "thinker"},
                {"providerId": "router-generalist", "candidate": "generalist"},
                {"providerId": "router-cloudflare", "candidate": None},
            ]
            table = compile_table.compile_table(INITIAL_PROGRAM, data, catalog, 0.5, "run-1", False)
        self.assertEqual(table["schema"], "immaculate.route-order-table.v1")
        self.assertEqual(table["unmeasured"], ["router-cloudflare"])
        rows = {(row["taskClass"], row["complexity"]): row["order"] for row in table["rows"]}
        self.assertEqual(rows[("codegen", "trivial")][0], "router-coder")
        self.assertEqual(rows[("reasoning", "trivial")][0], "router-thinker")
        self.assertIn(("*", "*"), rows)
        self.assertGreaterEqual(table["fitness"]["holdout"], table["fitness"]["bestSingleHoldout"])
        self.assertFalse(table["fitness"]["regressionAllowed"])

    def test_refuses_a_table_that_loses_to_the_best_single_candidate_on_the_holdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            write_folds(data, specialist_world())
            worst = Path(tmp) / "worst.py"
            # Sends each class to the other class's specialist: worse than always using the generalist.
            worst.write_text(
                "def rank_candidates(cell, stats, params):\n"
                "    return ['coder'] if cell['task_class'] == 'reasoning' else ['thinker']\n",
                encoding="utf-8",
            )
            catalog = [{"providerId": "p-" + name, "candidate": name} for name in ("coder", "thinker", "generalist")]
            with self.assertRaises(rd.DataError):
                compile_table.compile_table(worst, data, catalog, 0.5, None, False)
            allowed = compile_table.compile_table(worst, data, catalog, 0.5, None, True)
        self.assertTrue(allowed["fitness"]["regressionAllowed"])

    def test_catalog_must_map_to_measured_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            write_folds(data, specialist_world())
            with self.assertRaises(rd.DataError):
                compile_table.compile_table(INITIAL_PROGRAM, data, [{"providerId": "x", "candidate": "ghost"}], 0.5, None, False)


class FakeXRouteFrame:
    def __init__(self, records):
        self.records = records

    def __getitem__(self, column):
        return [record[column] for record in self.records]

    def itertuples(self, index=False):
        return [SimpleNamespace(**record) for record in self.records]


class FakeRouterBenchFrame:
    def __init__(self, records):
        self.records = records
        self.columns = list(records[0].keys())

    def to_dict(self, orient):
        assert orient == "records"
        return self.records


class PrepareTests(unittest.TestCase):
    def test_xroutebench_rows_price_measured_tokens_and_classify(self):
        frame = FakeXRouteFrame([
            {"task_name": "mbpp", "task_id": "654", "metric": "code_eval", "query": "Write a function", "model_name": "gpt-oss-120b",
             "input_tokens": 1000, "output_tokens": 500, "response_time": 0.75, "performance": 1.0},
        ])
        classify = prepare._Classifier({})
        rows = prepare.xroutebench_rows(frame, {"gpt-oss-120b": (0.15, 0.6)}, classify)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].task_key, "mbpp:654")
        self.assertEqual(rows[0].task_class, "codegen")
        self.assertAlmostEqual(rows[0].cost_usd, 1000 * 0.15 / 1e6 + 500 * 0.6 / 1e6)
        self.assertEqual(classify.decided["mbpp"]["rule"], "task-name:mbpp")
        with self.assertRaises(rd.DataError):
            prepare.xroutebench_rows(frame, {}, classify)

    def test_routerbench_rows_pair_scores_with_costs(self):
        frame = FakeRouterBenchFrame([
            {"sample_id": "s1", "prompt": "What is 2+2?", "eval_name": "gsm8k",
             "gpt-4": 1.0, "gpt-4|total_cost": 0.003, "gpt-4|model_response": "4",
             "mixtral": 0.0, "mixtral|total_cost": 0.0002, "mixtral|model_response": "5"},
        ])
        rows, models = prepare.routerbench_rows(frame, prepare._Classifier({}))
        self.assertEqual(models, ["gpt-4", "mixtral"])
        self.assertEqual({row.candidate: row.performance for row in rows}, {"gpt-4": 1.0, "mixtral": 0.0})
        self.assertTrue(all(row.task_class == "reasoning" for row in rows))

    def test_routerbench_refuses_to_unpickle_an_unpinned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pickle_path = Path(tmp) / "routerbench.pkl"
            pickle_path.write_bytes(b"not the published file")
            with self.assertRaises(SystemExit):
                prepare.main(["routerbench", "--pickle", str(pickle_path), "--expect-sha256", "0" * 64, "--out", tmp])


class ScoreResponsesTests(unittest.TestCase):
    def test_metric_scorers(self):
        choices = "{'text': ['Paris', 'Rome', 'Berlin'], 'labels': ['A', 'B', 'C']}"
        self.assertEqual(score_responses.score("em_mc", "The answer is (A).", "A", choices), 1.0)
        self.assertEqual(score_responses.score("em_mc", "B", "Paris", choices), 0.0)
        self.assertEqual(score_responses.score("em_mc", "A) Paris", "Paris", choices), 1.0)
        self.assertEqual(score_responses.score("GSM8K", "so she has $1,250 left", "#### 1250", None), 1.0)
        self.assertEqual(score_responses.score("GSM8K", "about 12", "13", None), 0.0)
        self.assertEqual(score_responses.score("MATH", "thus \\boxed{\\dfrac{1}{2}}", "\\frac{1}{2}", None), 1.0)
        self.assertEqual(score_responses.score("MATH", "no box here: 1/2", "\\frac{1}{2}", None), 0.0)
        self.assertAlmostEqual(score_responses.score("f1", "the Eiffel Tower", '["Eiffel Tower", "tower"]', None), 1.0)
        self.assertEqual(score_responses.score("em", "Paris.", "paris", None), 1.0)
        with self.assertRaises(rd.DataError):
            score_responses.score("bleu", "x", "y", None)

    def test_errors_score_zero_unscorable_metrics_are_skipped_and_missing_usage_is_estimated(self):
        queries = {
            "gsm8k:1": {"task_key": "gsm8k:1", "task_name": "gsm8k", "metric": "GSM8K", "query": "2+2?", "ground_truth": "4", "choices": None},
            "mbpp:1": {"task_key": "mbpp:1", "task_name": "mbpp", "metric": "code_eval", "query": "write", "ground_truth": "[]", "choices": None},
        }
        responses = [
            {"task_key": "gsm8k:1", "providerId": "router-groq", "response": "4", "usage": {"prompt_tokens": 10, "completion_tokens": 2}, "latencyMs": 300},
            {"task_key": "gsm8k:1", "providerId": "router-mistral", "response": "", "error": "router-mistral_http_429", "latencyMs": 50},
            {"task_key": "gsm8k:1", "providerId": "router-free", "response": "4", "usage": None, "latencyMs": 900},
            {"task_key": "mbpp:1", "providerId": "router-groq", "response": "def f(): pass", "latencyMs": 400},
        ]
        prices = {"router-groq": {"input": 0.15, "output": 0.6}, "router-mistral": {"input": 0.1, "output": 0.3}, "router-free": {"input": 0, "output": 0}}
        outcomes, summary = score_responses.score_responses(queries, responses, prices)
        by_provider = {row.candidate: row for row in outcomes}
        self.assertEqual(by_provider["router-groq"].performance, 1.0)
        self.assertAlmostEqual(by_provider["router-groq"].cost_usd, 10 * 0.15 / 1e6 + 2 * 0.6 / 1e6)
        self.assertEqual(by_provider["router-mistral"].performance, 0.0)
        self.assertEqual(by_provider["router-free"].cost_usd, 0.0)
        self.assertEqual(summary, {"scored": 3, "provider_errors": 1, "tokens_estimated": 1, "skipped:code_eval": 1})
        with self.assertRaises(rd.DataError):
            score_responses.score_responses(queries, [{**responses[0], "providerId": "router-unpriced"}], prices)


class ExperimentConfigTests(unittest.TestCase):
    def test_the_evolution_loop_calls_models_through_the_q_gateway(self):
        spec = importlib.util.spec_from_file_location("evolve_config_for_route_policy", EXPERIMENT.parents[1] / "utils" / "config.py")
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        env = {"IMMACULATE_Q_GATEWAY_BASE_URL": "http://127.0.0.1:8897/v1", "IMMACULATE_Q_API_KEY": "q-key-for-test"}
        with mock.patch.dict(os.environ, env):
            resolved = config.load_config(experiment_name="immaculate_route_policy")
        self.assertEqual(resolved["api"]["base_url"], "http://127.0.0.1:8897/v1")
        self.assertEqual(resolved["api"]["api_key"], "q-key-for-test")
        self.assertEqual(resolved["api"]["model"], "Q")
        self.assertFalse(resolved["logging"]["wandb"]["enabled"])

    def test_the_run_spec_template_is_unconfirmed(self):
        text = (EXPERIMENT / "run_spec.template.yaml").read_text(encoding="utf-8")
        self.assertIn("approval:\n  confirmed: false", text)
        self.assertIn('core_score: "fitness"', text)
        self.assertIn("timeout_secs: 120", text)


if __name__ == "__main__":
    unittest.main()
