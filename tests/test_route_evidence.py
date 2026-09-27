import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arobi_integrations import route_evidence as ev


def catalog_file(directory: Path, providers) -> Path:
    path = directory / "catalog.json"
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return path


GROQ = {"providerId": "router-groq", "benchmarkModel": "gpt-oss-120b", "inputPer1M": 0.15, "outputPer1M": 0.75}
CEREBRAS = {"providerId": "router-cerebras", "benchmarkModel": "gpt-oss-120b", "inputPer1M": 0.25, "outputPer1M": 0.69}
CLOUDFLARE = {"providerId": "router-cloudflare", "benchmarkModel": None, "inputPer1M": 0, "outputPer1M": 0}


class ClassificationTests(unittest.TestCase):
    def test_benchmark_tasks_map_onto_darwin_route_task_classes(self):
        self.assertEqual(ev.classify_task("mbpp", "code_eval"), ("coding", "task-name:mbpp"))
        self.assertEqual(ev.classify_task("gsm8k", "GSM8K")[0], "reasoning")
        self.assertEqual(ev.classify_task("mmlu_high_school_physics", "em_mc")[0], "research")
        self.assertEqual(ev.classify_task("squad", "f1")[0], "extraction")
        self.assertEqual(ev.classify_task("mt_bench", "llm_judge")[0], "conversation")
        self.assertEqual(ev.classify_task("unfamiliar", "f1"), ("extraction", "metric:f1"))
        self.assertEqual(ev.classify_task("mmlu", "em_mc", {"mmlu": "reasoning"}), ("reasoning", "class-map"))
        with self.assertRaises(ev.EvidenceError):
            ev.classify_task("unfamiliar", "bleu")
        with self.assertRaises(ev.EvidenceError):
            ev.classify_task("mmlu", "em_mc", {"mmlu": "astrology"})
        self.assertEqual(set(ev.TASK_CLASSES), {"coding", "research", "reasoning", "extraction", "conversation"})


class CatalogTests(unittest.TestCase):
    def test_catalog_requires_unique_ids_and_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(len(ev.load_catalog(catalog_file(directory, [GROQ, CLOUDFLARE]))), 2)
            for bad in ([GROQ, GROQ], [{"providerId": "x", "benchmarkModel": "m"}], [{**GROQ, "inputPer1M": -1}], []):
                with self.assertRaises(ev.EvidenceError):
                    ev.load_catalog(catalog_file(directory, bad))


class ObservationTests(unittest.TestCase):
    def test_xroutebench_rows_become_observations_only_for_providers_serving_that_model(self):
        records = [
            SimpleNamespace(task_name="mbpp", metric="code_eval", model_name="gpt-oss-120b", performance=1.0, input_tokens=1000, output_tokens=200),
            SimpleNamespace(task_name="gsm8k", metric="GSM8K", model_name="gpt-oss-120b", performance=0.0, input_tokens=500, output_tokens=100),
            SimpleNamespace(task_name="mbpp", metric="code_eval", model_name="deepseek-v3.1", performance=1.0, input_tokens=1000, output_tokens=200),
        ]
        rows = ev.xroutebench_observations(records, [GROQ, CEREBRAS, CLOUDFLARE], ev._Classifier({}), "xroutebench:train")
        self.assertEqual(len(rows), 4, "two gpt-oss-120b items x two providers; no provider serves deepseek")
        groq_code = next(row for row in rows if row["providerId"] == "router-groq" and row["taskClass"] == "coding")
        self.assertEqual(groq_code["quality"], 1.0)
        self.assertTrue(groq_code["ok"])
        self.assertIsNone(groq_code["latencyMs"], "the benchmark host's latency is not our provider's")
        self.assertAlmostEqual(groq_code["costUsd"], 1000 * 0.15 / 1e6 + 200 * 0.75 / 1e6)
        cerebras_code = next(row for row in rows if row["providerId"] == "router-cerebras" and row["taskClass"] == "coding")
        self.assertAlmostEqual(cerebras_code["costUsd"], 1000 * 0.25 / 1e6 + 200 * 0.69 / 1e6, msg="each provider's own price")
        self.assertEqual({row["providerId"] for row in rows}, {"router-groq", "router-cerebras"})

    def test_routerbench_scores_become_quality_with_unknown_cost(self):
        records = [{"eval_name": "gsm8k", "gpt-oss-120b": 1.0, "gpt-4": 0.0}, {"eval_name": "mbpp", "gpt-oss-120b": float("nan")}]
        rows = ev.routerbench_observations(records, [GROQ], ev._Classifier({}), "routerbench")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["costUsd"])
        self.assertEqual(rows[0]["taskClass"], "reasoning")

    def test_output_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "obs.jsonl"
            rows = [ev.observation("coding", GROQ, 1.0, 10, 5, "test")]
            manifest = ev.write_output(out, rows, {"source": "test"})
            self.assertEqual(manifest["observations"], 1)
            self.assertEqual(manifest["cells"], {"coding/router-groq": 1})
            self.assertEqual(json.loads(out.read_text().strip())["providerId"], "router-groq")
            self.assertTrue(out.with_suffix(".jsonl.manifest.json").exists())
            with self.assertRaises(ev.EvidenceError):
                ev.write_output(out, [], {})

    def test_routerbench_refuses_an_unpinned_pickle(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pickle_path = directory / "routerbench.pkl"
            pickle_path.write_bytes(b"not the published file")
            with self.assertRaises(SystemExit):
                ev.main(["routerbench", "--pickle", str(pickle_path), "--expect-sha256", "0" * 64,
                         "--catalog", str(catalog_file(directory, [GROQ])), "--out", str(directory / "o.jsonl")])


if __name__ == "__main__":
    unittest.main()
