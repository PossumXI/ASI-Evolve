import ast
import contextlib
import gzip
import hashlib
import io
import json
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from arobi_integrations import route_evidence as ev

# Synthetic prices for the tests only; the operator's catalog carries the real contract prices.
GROQ = {"providerId": "router-groq", "servedModel": "openai/gpt-oss-120b", "inputPer1M": 0.15, "outputPer1M": 0.75}
CEREBRAS = {"providerId": "router-cerebras", "servedModel": "gpt-oss-120b", "inputPer1M": 0.25, "outputPer1M": 0.69}
HUGGINGFACE = {"providerId": "router-huggingface", "servedModel": "openai/gpt-oss-120b:fastest", "inputPer1M": 0.1, "outputPer1M": 0.5}
FIREWORKS = {"providerId": "router-fireworks", "servedModel": "accounts/fireworks/models/gpt-oss-120b", "inputPer1M": 0.15, "outputPer1M": 0.6}
CLOUDFLARE = {"providerId": "router-cloudflare", "servedModel": None, "inputPer1M": 0, "outputPer1M": 0}
QWEN_HOST = {"providerId": "router-qwen-host", "servedModel": "qwen/Qwen2.5-7B-Instruct", "inputPer1M": 0.2, "outputPer1M": 0.2}
LLAMA_HOST = {"providerId": "router-llama-host", "servedModel": "meta-llama/Llama-3.1-8B-Instruct", "inputPer1M": 0.1, "outputPer1M": 0.1}


def full_commit(spec):
    return spec.commit + "0" * (40 - len(spec.commit))


def jsonl(rows):
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")


def catalog_file(directory: Path, providers) -> Path:
    path = directory / "catalog.json"
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return path


def source_entry(spec, **overrides):
    entry = {
        "repo": spec.repo,
        "commit": full_commit(spec),
        "license": {"card": spec.licence.lower(), "repoFile": {"path": "LICENSE", "spdx": spec.licence}},
    }
    entry.update(overrides)
    return entry


def pinned(directory: Path, spec, payload: bytes, source=None, download=None, extra_sources=()):
    """Write a data file plus MANIFEST-SOURCES.json and DOWNLOADS.json pinning it; return their paths."""
    data = directory / Path(spec.path).name
    data.write_bytes(payload)
    download_entry = {
        "repo": spec.repo,
        "commit": full_commit(spec),
        "path": spec.path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    download_entry.update(download or {})
    sources = directory / "MANIFEST-SOURCES.json"
    sources.write_text(json.dumps({"sources": [source or source_entry(spec), *extra_sources]}), encoding="utf-8")
    downloads = directory / "DOWNLOADS.json"
    downloads.write_text(json.dumps({"downloads": [download_entry]}), encoding="utf-8")
    return sources, downloads, data


def eljefe_row(index, task, score=1.0, **overrides):
    row = {
        "id": f"eljefe-{index}",
        "source": task,
        "task_family": "synthetic",
        "prompt": "synthetic prompt text",
        "frontier_model": "accounts/fireworks/models/gpt-oss-120b",
        "frontier_score": score,
        "frontier_input_tokens": 1000,
        "frontier_output_tokens": 200,
        "frontier_cost": 99.0,
        "frontier_latency_ms": 1234,
        "grader_type": "exact_match",
        "grader_confidence": 0.9,
        "local_model": "gemma-4-E4B-it",
        "local_score": 0.0,
        "local_input_tokens": 1000,
        "local_output_tokens": 300,
        "local_cost": 0.0,
        "local_latency_ms": 5000,
    }
    row.update(overrides)
    return row


def dataset_a_row(query_id, source, qwen=True, ds4=False, kimi=True, **overrides):
    row = {
        "query_id": query_id,
        "query": "synthetic query text",
        "dimension": "synthetic",
        "evaluation_protocol_id": f"{source}-protocol",
        "source": source,
        "gated": False,
        "qwen_correct": qwen,
        "ds4_correct": ds4,
        "kimi_correct": kimi,
    }
    row.update(overrides)
    return row


def icl_row(index, model, task, correct=True):
    return {"query": "synthetic query text", "model": model, "is_correct_direct": correct, "is_correct": [correct], "task": task, "index": index}


def darwin_route_accepts(record) -> bool:
    """The checks Immaculate's darwin-route-cli.ts observationFrom applies to every --observations line."""
    def finite_or_null(value):
        return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))

    quality = record.get("quality")
    return (
        bool(record.get("taskClass"))
        and bool(record.get("providerId"))
        and isinstance(record.get("ok"), bool)
        and finite_or_null(record.get("latencyMs"))
        and finite_or_null(record.get("costUsd"))
        and (record.get("costUsd") is None or record["costUsd"] >= 0)
        and (quality is None or 0 <= quality <= 1)
    )


def run_main(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = ev.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ClassificationTests(unittest.TestCase):
    def test_owner_task_mapping_onto_darwin_route_task_classes(self):
        self.assertEqual(ev.classify_task("mbpp"), ("coding", "owner-map:mbpp->coding"))
        for task in ("gsm8k", "math500", "mmlu_pro", "MMLU-Pro", "MATH-500", "GSM8K"):
            with self.subTest(task=task):
                self.assertEqual(ev.classify_task(task)[0], "reasoning")
        self.assertEqual(ev.classify_task("ifeval"), ("extraction", "owner-map:ifeval->extraction"))
        self.assertEqual(ev.classify_task("IFEval")[0], "extraction")
        for unmapped in ("mmlu", "math", "humaneval", "aime_2025", "", None):
            with self.subTest(task=unmapped):
                self.assertEqual(ev.classify_task(unmapped), (None, "unmapped: not in the owner's class map"))
        self.assertEqual(set(ev.TASK_CLASSES), {"coding", "research", "reasoning", "extraction", "conversation"})
        self.assertTrue(set(ev.OWNER_CLASS_MAP.values()) <= set(ev.TASK_CLASSES))
        for line in ("mbpp                        -> coding", "gsm8k, math500, mmlu_pro    -> reasoning", "ifeval                      -> extraction"):
            self.assertIn(line, ev.__doc__, "the module docstring states the owner's mapping")

    def test_model_names_are_compared_canonically(self):
        for served in ("openai/gpt-oss-120b", "gpt-oss-120b", "openai/gpt-oss-120b:fastest", "accounts/fireworks/models/gpt-oss-120b"):
            self.assertEqual(ev.canonical_model(served), "gpt-oss-120b")
        llamas = (
            "Llama-3.1-8B-Instruct", "meta-llama/Meta-Llama-3.1-70B-Instruct", "llama3.1-8b", "llama3.1:8b", "Llama-3.3-70B",
            "meta-llama/anything", "CodeLlama-7b-Instruct-hf", "Llama-Guard-3-8B", "DeepSeek-R1-Distill-Llama-8B",
            "nvidia/Llama-3.1-Nemotron-70B-Instruct",
        )
        for llama in llamas:
            with self.subTest(model=llama):
                self.assertTrue(ev.is_meta_llama(llama))
        for other in ("Qwen2.5-7B-Instruct", "gpt-oss-120b", "TinyLlama-1.1B", "gemma-4-E4B-it", "Mistral-7B-Instruct-v0.3", "", None):
            with self.subTest(model=other):
                self.assertFalse(ev.is_meta_llama(other))


class CatalogTests(unittest.TestCase):
    def test_catalog_requires_unique_ids_and_finite_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            catalog = ev.load_catalog(catalog_file(directory, [GROQ, CLOUDFLARE]))
            self.assertEqual([entry["servedModel"] for entry in catalog], ["openai/gpt-oss-120b", None])
            bad_catalogs = (
                [GROQ, GROQ],
                [{"providerId": "x", "servedModel": "m"}],
                [{**GROQ, "inputPer1M": -1}],
                [{**GROQ, "outputPer1M": float("nan")}],
                [],
            )
            for bad in bad_catalogs:
                with self.subTest(catalog=bad), self.assertRaises(ev.EvidenceError):
                    ev.load_catalog(catalog_file(directory, bad))


class LicenceGateTests(unittest.TestCase):
    def test_every_allowlisted_licence_passes_when_card_and_repo_file_agree(self):
        for spdx in ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC-BY-4.0", "CC0-1.0", "Unlicense"):
            with self.subTest(licence=spdx):
                entry = {"repo": "someone/dataset", "license": {"card": spdx.lower(), "repoFile": {"path": "LICENSE", "spdx": spdx}}}
                self.assertEqual(ev.check_licence(entry)["spdx"], spdx)

    def test_refuses_anything_not_verified_from_both_card_and_repo_file(self):
        def entry(card, repo_file):
            licence = {"card": card}
            if repo_file is not None:
                licence["repoFile"] = repo_file
            return {"repo": "someone/dataset", "license": licence}

        refused = {
            "no licence evidence": {"repo": "someone/dataset"},
            "card declares none": entry(None, {"path": "LICENSE", "spdx": "MIT"}),
            "card non-commercial": entry("cc-by-nc-4.0", {"path": "LICENSE", "spdx": "CC-BY-NC-4.0"}),
            "card copyleft": entry("gpl-3.0", {"path": "LICENSE", "spdx": "GPL-3.0-only"}),
            "card 'other'": entry("other", {"path": "LICENSE", "spdx": "MIT"}),
            "card SPDX expression": entry("MIT OR GPL-3.0", {"path": "LICENSE", "spdx": "MIT"}),
            "ambiguous bsd": entry("bsd", {"path": "LICENSE", "spdx": "bsd"}),
            "no repo file": entry("mit", None),
            "repo file is the card": entry("mit", {"path": "README.md", "spdx": "MIT"}),
            "repo file not permissive": entry("mit", {"path": "LICENSE", "spdx": "CC-BY-SA-4.0"}),
            "card and repo file disagree": entry("mit", {"path": "LICENSE", "spdx": "Apache-2.0"}),
        }
        for name, bad in refused.items():
            with self.subTest(case=name), self.assertRaises(ev.LicenceError):
                ev.check_licence(bad)
        with self.assertRaises(ev.LicenceError) as caught:
            ev.check_licence(entry("cc-by-nc-4.0", {"path": "LICENSE", "spdx": "CC-BY-NC-4.0"}))
        self.assertIn("permissive licence", str(caught.exception))

    def test_several_repo_licence_files_must_all_be_allowed_and_agree(self):
        agree = {"card": "apache-2.0", "repoFiles": [{"path": "LICENSE", "spdx": "Apache-2.0"}, {"path": "data/LICENSE.txt", "spdx": "apache-2.0"}]}
        self.assertEqual([f["path"] for f in ev.check_licence({"repo": "someone/dataset", "license": agree})["repoFiles"]],
                         ["LICENSE", "data/LICENSE.txt"])
        both = {"card": "mit", "repoFile": {"path": "LICENSE", "spdx": "MIT"}, "repoFiles": [{"path": "COPYING", "spdx": "MIT"}]}
        self.assertEqual(len(ev.check_licence({"repo": "someone/dataset", "license": both})["repoFiles"]), 2)
        refused = {
            "one file disagrees": {"card": "mit", "repoFiles": [{"path": "LICENSE", "spdx": "MIT"}, {"path": "data/LICENSE", "spdx": "GPL-3.0-only"}]},
            "repoFiles not a list": {"card": "mit", "repoFiles": {"path": "LICENSE", "spdx": "MIT"}},
            "empty repoFiles": {"card": "mit", "repoFiles": []},
            "a file without a path": {"card": "mit", "repoFiles": [{"spdx": "MIT"}]},
            "README.rst is the card": {"card": "mit", "repoFile": {"path": "README.rst", "spdx": "MIT"}},
            "nested README is the card": {"card": "mit", "repoFile": {"path": "docs/README.md", "spdx": "MIT"}},
            "dataset_card.md is the card": {"card": "mit", "repoFile": {"path": "dataset_card.md", "spdx": "MIT"}},
            "licence file sha256 not hex": {"card": "mit", "repoFile": {"path": "LICENSE", "spdx": "MIT", "sha256": "abc"}},
        }
        for name, licence in refused.items():
            with self.subTest(case=name), self.assertRaises(ev.LicenceError):
                ev.check_licence({"repo": "someone/dataset", "license": licence})

    def test_licence_may_be_spelled_either_way_but_not_recorded_twice_differently(self):
        evidence = {"card": "cc-by-4.0", "repoFile": {"path": "LICENSE", "spdx": "CC-BY-4.0"}}
        self.assertEqual(ev.check_licence({"repo": "someone/dataset", "licence": evidence})["spdx"], "CC-BY-4.0")
        self.assertEqual(ev.check_licence({"repo": "someone/dataset", "license": evidence, "licence": evidence})["spdx"], "CC-BY-4.0")
        with self.assertRaises(ev.LicenceError):
            ev.check_licence({"repo": "someone/dataset", "license": evidence,
                              "licence": {"card": "cc-by-nc-4.0", "repoFile": {"path": "LICENSE", "spdx": "CC-BY-NC-4.0"}}})

    def test_unlicensed_benchmarks_are_refused_even_when_a_manifest_claims_a_licence(self):
        for command, (name, repo) in ev.UNLICENSED_SOURCES.items():
            owner, dataset = repo.split("/")
            spellings = (
                repo.upper(), f"datasets/{repo}", f"hf://datasets/{repo}", f"https://huggingface.co/datasets/{repo}/",
                f"https://github.com/{owner}/{dataset}.git", f"  {owner.lower()}/{dataset.lower()}  ",
            )
            for spelling in spellings:
                with self.subTest(source=name, repo=spelling):
                    claim = {"repo": spelling, "license": {"card": "mit", "repoFile": {"path": "LICENSE", "spdx": "MIT"}}}
                    with self.assertRaises(ev.LicenceError) as caught:
                        ev.check_licence(claim)
                    self.assertIn("declares no licence", str(caught.exception))
                    self.assertIn("permissive licence", str(caught.exception))

    def test_repository_references_normalise_only_known_prefixes(self):
        for spelling in ("DJLougen/eljefe-router-data", "datasets/DJLougen/eljefe-router-data",
                         "hf://datasets/DJLougen/eljefe-router-data", "https://huggingface.co/datasets/DJLougen/eljefe-router-data"):
            with self.subTest(repo=spelling):
                self.assertEqual(ev.normalise_repo(spelling), "djlougen/eljefe-router-data")
        self.assertEqual(ev.normalise_repo("https://github.com/lalalamdbf/ICL-Router.git"), "lalalamdbf/icl-router")
        self.assertNotEqual(ev.normalise_repo("https://mirror.example/DJLougen/eljefe-router-data"), "djlougen/eljefe-router-data",
                            "an unknown host is not the Hub or GitHub")

    def test_unlicensed_benchmark_commands_refuse_without_reading_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pickle_path = directory / "routerbench_0shot.pkl"
            pickle_path.write_bytes(b"not a pickle, and never opened")
            invocations = {
                "xroutebench": ["--rows", str(directory / "train.parquet"), "--catalog", "c.json", "--out", str(directory / "o.jsonl")],
                "routerbench": ["--pickle", str(pickle_path), "--expect-sha256", hashlib.sha256(pickle_path.read_bytes()).hexdigest(),
                                "--catalog", "c.json", "--out", str(directory / "o.jsonl")],
                "llmrouterbench": ["--tarball", str(directory / "bench-release.tar.gz")],
                "xRouteBench": ["--out", str(directory / "o.jsonl")],
                "router_bench": ["--out", str(directory / "o.jsonl")],
                "LLM-Router-Bench": ["--out", str(directory / "o.jsonl")],
            }
            for command, rest in invocations.items():
                with self.subTest(command=command):
                    code, stdout, stderr = run_main([command, *rest])
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertIn("declares no licence", stderr)
                    self.assertIn("MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, CC-BY-4.0, CC0-1.0 or Unlicense", stderr)
            self.assertFalse((directory / "o.jsonl").exists())
            self.assertFalse(hasattr(ev, "xroutebench_observations") or hasattr(ev, "routerbench_observations"),
                             "the unlicensed adapters are removed, not just unreachable")

    def test_adapter_refuses_a_licence_other_than_the_one_it_was_pinned_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            mit = source_entry(ev.ELJEFE, license={"card": "mit", "repoFile": {"path": "LICENSE", "spdx": "MIT"}})
            sources, downloads, data = pinned(directory, ev.ELJEFE, jsonl([eljefe_row(1, "mbpp")]), source=mit)
            with self.assertRaises(ev.LicenceError):
                ev.verify_source(ev.ELJEFE, ev.load_manifest_entries(sources, "sources"), ev.load_manifest_entries(downloads, "downloads"), data)


class PinningTests(unittest.TestCase):
    def verify(self, directory, spec, payload, **kwargs):
        sources, downloads, data = pinned(directory, spec, payload, **kwargs)
        return ev.verify_source(spec, ev.load_manifest_entries(sources, "sources"), ev.load_manifest_entries(downloads, "downloads"), data)

    def test_a_pinned_file_passes_and_its_provenance_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = jsonl([eljefe_row(1, "mbpp")])
            provenance = self.verify(Path(tmp), ev.ELJEFE, payload)
            self.assertEqual(provenance["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(provenance["commit"], full_commit(ev.ELJEFE))
            self.assertEqual(provenance["licence"]["spdx"], "Apache-2.0")
            self.assertEqual(provenance["licence"]["repoFiles"], [{"path": "LICENSE", "spdx": "Apache-2.0"}])
            self.assertIn("Apache-2.0", provenance["attribution"])
            self.assertEqual(provenance["licence_url"], "https://spdx.org/licenses/Apache-2.0.html")
            self.assertIn("content-free", provenance["changes"], "the manifest says how the material was changed")

    def test_sha256_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ev.PinError) as caught:
                self.verify(Path(tmp), ev.ELJEFE, jsonl([eljefe_row(1, "mbpp")]), download={"sha256": "0" * 64, "bytes": None})
            self.assertIn("sha256", str(caught.exception))

    def test_every_other_pin_violation_is_refused(self):
        payload = jsonl([eljefe_row(1, "mbpp")])
        other_commit = "deadbeef" + "0" * 32
        violations = {
            "source manifest pins another commit": {"source": source_entry(ev.ELJEFE, commit=other_commit)},
            "commit is not hex": {"source": source_entry(ev.ELJEFE, commit="main")},
            "download made at another commit": {"download": {"commit": other_commit}},
            "sha256 missing": {"download": {"sha256": ""}},
            "byte count differs": {"download": {"bytes": len(payload) + 1}},
            "download pins a different file": {"download": {"path": "other.jsonl"}},
        }
        for name, kwargs in violations.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ev.PinError):
                    self.verify(Path(tmp), ev.ELJEFE, payload, **kwargs)

    def test_commits_agree_by_prefix_and_may_be_recorded_as_revision(self):
        payload = jsonl([eljefe_row(1, "mbpp")])
        short = ev.ELJEFE.commit[:7]
        accepted = {
            "7-hex short commit in the source manifest": {"source": source_entry(ev.ELJEFE, commit=short)},
            "revision instead of commit": {"source": {k: v for k, v in source_entry(ev.ELJEFE).items() if k != "commit"} | {"revision": full_commit(ev.ELJEFE)}},
            "commit and revision both recorded, agreeing": {"source": source_entry(ev.ELJEFE, revision=full_commit(ev.ELJEFE))},
            "repository written as a Hub URL in both manifests": {
                "source": source_entry(ev.ELJEFE, repo=f"https://huggingface.co/datasets/{ev.ELJEFE.repo}"),
                "download": {"repo": f"datasets/{ev.ELJEFE.repo}"},
            },
        }
        for name, kwargs in accepted.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(self.verify(Path(tmp), ev.ELJEFE, payload, **kwargs)["sha256"], hashlib.sha256(payload).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            provenance = self.verify(Path(tmp), ev.ELJEFE, payload, source=source_entry(ev.ELJEFE, commit=short), download={"commit": short})
            self.assertEqual(provenance["commit"], ev.ELJEFE.commit, "the longest agreeing id is recorded")
        refused = {
            "commit and revision disagree": {"source": source_entry(ev.ELJEFE, revision="deadbeef" + "0" * 32)},
            "6-hex commit is too short to pin": {"source": source_entry(ev.ELJEFE, commit=ev.ELJEFE.commit[:6])},
            "download agrees with a short manifest commit but not with the adapter's revision": {
                "source": source_entry(ev.ELJEFE, commit=short),
                "download": {"commit": short + "0" + "0" * 32},
            },
        }
        for name, kwargs in refused.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ev.PinError):
                    self.verify(Path(tmp), ev.ELJEFE, payload, **kwargs)

    def test_missing_or_duplicate_manifest_entries_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(ev.PinError):
                self.verify(directory, ev.ELJEFE, b"{}\n", source=source_entry(ev.ICL_ROUTER))
            with self.assertRaises(ev.PinError):
                self.verify(directory, ev.ELJEFE, b"{}\n", extra_sources=[source_entry(ev.ELJEFE)])
            with self.assertRaises(ev.PinError, msg="the same repository spelled two ways is still two entries"):
                self.verify(directory, ev.ELJEFE, b"{}\n", extra_sources=[source_entry(ev.ELJEFE, repo=f"hf://datasets/{ev.ELJEFE.repo}")])

    def test_manifests_may_be_bare_lists_or_one_combined_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = jsonl([eljefe_row(1, "mbpp")])
            _, downloads, data = pinned(directory, ev.ELJEFE, payload)
            combined = directory / "combined.json"
            combined.write_text(json.dumps({"sources": [source_entry(ev.ELJEFE)], "downloads": ev.load_manifest_entries(downloads, "downloads")}))
            bare = directory / "bare-downloads.json"
            bare.write_text(json.dumps(ev.load_manifest_entries(downloads, "downloads")))
            provenance = ev.verify_source(ev.ELJEFE, ev.load_manifest_entries(combined, "sources"), ev.load_manifest_entries(bare, "downloads"), data)
            self.assertEqual(provenance["sha256"], hashlib.sha256(payload).hexdigest())
            bad = directory / "bad.json"
            bad.write_text(json.dumps({"downloads": "not a list"}))
            with self.assertRaises(ev.PinError):
                ev.load_manifest_entries(bad, "downloads")

    def test_cli_refuses_a_mismatched_file_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sources, downloads, data = pinned(directory, ev.ELJEFE, jsonl([eljefe_row(1, "mbpp")]))
            data.write_bytes(jsonl([eljefe_row(1, "mbpp", score=0.0)]))
            out = directory / "obs.jsonl"
            code, _, stderr = run_main(["eljefe-router-data", "--sources", str(sources), "--downloads", str(downloads), "--file", str(data),
                                        "--catalog", str(catalog_file(directory, [GROQ])), "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertIn("refused:", stderr)
            self.assertFalse(out.exists())

    def test_cli_refuses_a_row_count_other_than_the_pinned_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sources, downloads, data = pinned(directory, ev.ELJEFE, jsonl([eljefe_row(1, "mbpp")]), download={"rows": 997})
            out = directory / "obs.jsonl"
            code, _, stderr = run_main(["eljefe-router-data", "--sources", str(sources), "--downloads", str(downloads), "--file", str(data),
                                        "--catalog", str(catalog_file(directory, [GROQ])), "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertIn("997", stderr)
            self.assertFalse(out.exists())

    def test_the_module_never_fetches_or_unpickles(self):
        tree = ast.parse(Path(ev.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"urllib", "http", "requests", "httpx", "socket", "huggingface_hub", "datasets", "pickle", "pandas"}
        self.assertEqual(imported & forbidden, set())


class EljefeTests(unittest.TestCase):
    ROWS = [
        eljefe_row(1, "mbpp", 1.0),
        eljefe_row(2, "gsm8k", 0.0),
        eljefe_row(3, "math500", 0.5),
        eljefe_row(4, "mmlu_pro", 1.0),
        eljefe_row(5, "ifeval", 0.75),
    ]

    def test_only_the_gpt_oss_120b_arm_seeds_groq_huggingface_and_cerebras(self):
        catalog = [GROQ, CEREBRAS, HUGGINGFACE, FIREWORKS, CLOUDFLARE, {**QWEN_HOST, "providerId": "router-groq-other"}]
        evidence = ev.eljefe_evidence(self.ROWS, catalog)
        self.assertEqual(len(evidence.observations), 15, "five items x the three named providers")
        self.assertEqual({row["providerId"] for row in evidence.observations}, {"router-groq", "router-cerebras", "router-huggingface"},
                         "Fireworks serves gpt-oss-120b too, but only the three named fallbacks may be seeded")
        self.assertTrue(all(row["ok"] is True for row in evidence.observations))
        self.assertEqual({row["quality"] for row in evidence.observations}, {1.0, 0.0, 0.5, 0.75})
        self.assertTrue(all(darwin_route_accepts(row) for row in evidence.observations))
        self.assertEqual(set(evidence.observations[0]), {"taskClass", "providerId", "ok", "latencyMs", "costUsd", "quality", "source"})

    def test_a_named_provider_serving_another_model_is_not_seeded(self):
        groq_on_llama = {**GROQ, "servedModel": "llama-3.3-70b-versatile"}
        evidence = ev.eljefe_evidence(self.ROWS, [groq_on_llama, CEREBRAS])
        self.assertEqual({row["providerId"] for row in evidence.observations}, {"router-cerebras"})
        with self.assertRaises(ev.EvidenceError):
            ev.eljefe_evidence(self.ROWS, [groq_on_llama, FIREWORKS, CLOUDFLARE])

    def test_latency_is_null_even_though_fireworks_latency_was_measured(self):
        evidence = ev.eljefe_evidence(self.ROWS, [GROQ, CEREBRAS, HUGGINGFACE])
        self.assertTrue(all(row["latencyMs"] is None for row in evidence.observations))

    def test_cost_is_each_providers_own_price_never_fireworks(self):
        evidence = ev.eljefe_evidence([eljefe_row(1, "mbpp")], [GROQ, CEREBRAS, HUGGINGFACE])
        cost = {row["providerId"]: row["costUsd"] for row in evidence.observations}
        self.assertAlmostEqual(cost["router-groq"], 1000 * 0.15 / 1e6 + 200 * 0.75 / 1e6)
        self.assertAlmostEqual(cost["router-cerebras"], 1000 * 0.25 / 1e6 + 200 * 0.69 / 1e6)
        self.assertAlmostEqual(cost["router-huggingface"], 1000 * 0.1 / 1e6 + 200 * 0.5 / 1e6)
        self.assertNotIn(99.0, cost.values(), "frontier_cost is Fireworks' price and is never read")
        no_tokens = ev.eljefe_evidence([eljefe_row(1, "mbpp", frontier_input_tokens=None)], [GROQ])
        self.assertIsNone(no_tokens.observations[0]["costUsd"], "unknown tokens mean unknown cost, not Fireworks' cost")

    def test_class_mapping_and_the_rule_that_decided_each_task(self):
        evidence = ev.eljefe_evidence(self.ROWS, [GROQ])
        self.assertEqual([row["source"] for row in self.ROWS], ["mbpp", "gsm8k", "math500", "mmlu_pro", "ifeval"])
        self.assertEqual([obs["taskClass"] for obs in evidence.observations], ["coding", "reasoning", "reasoning", "reasoning", "extraction"])
        self.assertEqual(evidence.task_classes["mmlupro"], {"taskClass": "reasoning", "rule": "owner-map:mmlupro->reasoning"})
        self.assertEqual(evidence.task_classes["ifeval"], {"taskClass": "extraction", "rule": "owner-map:ifeval->extraction"})

    def test_local_arm_unmapped_tasks_and_bad_scores_never_become_observations(self):
        rows = [eljefe_row(1, "mbpp"), eljefe_row(2, "humaneval"), eljefe_row(3, "gsm8k", score=1.7), eljefe_row(4, "gsm8k", score=None)]
        evidence = ev.eljefe_evidence(rows, [GROQ])
        self.assertEqual(len(evidence.observations), 1)
        self.assertEqual(evidence.dropped["row:invalid-frontier_score"], 2)
        reasons = Counter(record["reason"] for record in evidence.evaluation)
        self.assertEqual(reasons, {ev.REASON_NOT_SERVED: 4, ev.REASON_UNMAPPED: 1})
        local = [record for record in evidence.evaluation if record["model"] == "gemma-4-E4B-it"]
        self.assertEqual(len(local), 4)
        for record in evidence.evaluation:
            self.assertEqual(record["use"], "evaluation-only")
            self.assertNotIn("providerId", record)
            self.assertFalse(darwin_route_accepts(record), "Darwin-Route's loader must reject an evaluation line")
            self.assertNotIn("prompt", record)

    def test_cli_writes_labelled_observation_and_evaluation_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sources, downloads, data = pinned(directory, ev.ELJEFE, jsonl(self.ROWS), download={"rows": 5})
            out, evaluation_out = directory / "obs.jsonl", directory / "eval.jsonl"
            code, stdout, stderr = run_main([
                "eljefe-router-data", "--sources", str(sources), "--downloads", str(downloads), "--file", str(data),
                "--catalog", str(catalog_file(directory, [GROQ, CEREBRAS, HUGGINGFACE])), "--out", str(out), "--evaluation-out", str(evaluation_out),
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["observations"], 15)
            observations = read_jsonl(out)
            self.assertTrue(all(darwin_route_accepts(row) for row in observations))
            self.assertTrue(all("synthetic prompt text" not in json.dumps(row) for row in observations), "content-free")
            manifest = json.loads(out.with_suffix(".jsonl.manifest.json").read_text())
            self.assertEqual(manifest["use"], "darwin-route-observations")
            self.assertEqual(manifest["records"], 15)
            self.assertEqual(manifest["source"]["licence"]["spdx"], "Apache-2.0")
            self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(data.read_bytes()).hexdigest())
            self.assertEqual(manifest["task_classes"]["mbpp"]["rule"], "owner-map:mbpp->coding")
            self.assertIn("Fireworks", manifest["rules"]["latencyMs"])
            self.assertEqual(manifest["cells"]["coding/router-groq"], 1)
            evaluation = read_jsonl(evaluation_out)
            self.assertEqual(len(evaluation), 5)
            self.assertTrue(all(not darwin_route_accepts(record) for record in evaluation))
            evaluation_manifest = json.loads(evaluation_out.with_suffix(".jsonl.manifest.json").read_text())
            self.assertEqual(evaluation_manifest["use"], "evaluation-only")
            self.assertEqual(evaluation_manifest["reasons"], {ev.REASON_NOT_SERVED: 5})


class DatasetATests(unittest.TestCase):
    ROWS = [
        {"query_id": "_schema_anchor", "query": "", "dimension": "", "evaluation_protocol_id": "", "source": "",
         "gated": False, "qwen_correct": None, "ds4_correct": None, "kimi_correct": None},
        dataset_a_row("q1", "gsm8k"),
        dataset_a_row("q2", "AIME-2025"),
        dataset_a_row("q3", "livecodebench_v6"),
        dataset_a_row("q4", "math", evaluation_protocol_id="aime_2025_exact"),
        dataset_a_row("q5", "LiveCodeBench"),
        dataset_a_row("q6", "ifeval", kimi=None, gated=True),
        dataset_a_row("q7", "gpqa_diamond"),
    ]

    def test_drops_schema_anchor_aime_and_livecodebench_rows(self):
        evidence = ev.dataset_a_evidence(self.ROWS)
        self.assertEqual(evidence.dropped["row:schema-anchor"], 1)
        self.assertEqual(evidence.dropped["row:upstream-copyright:aime"], 2)
        self.assertEqual(evidence.dropped["row:upstream-copyright:livecodebench"], 2)
        self.assertEqual({record["item"] for record in evidence.evaluation}, {"q1", "q6", "q7"})
        self.assertEqual(evidence.counts["gated-rows"], 1)

    def test_copyright_is_detected_in_any_identifying_field_never_the_query(self):
        rows = [
            dataset_a_row("aime2025_07", "competition_math", evaluation_protocol_id="exact_match"),
            dataset_a_row("q10", "code", evaluation_protocol_id="unit_tests", dimension="lcb_v6"),
            dataset_a_row("q11", "gsm8k", query="a question that mentions AIME and LiveCodeBench in its text"),
        ]
        evidence = ev.dataset_a_evidence(rows)
        self.assertEqual(evidence.dropped["row:upstream-copyright:aime"], 1)
        self.assertEqual(evidence.dropped["row:upstream-copyright:livecodebench"], 1)
        self.assertEqual({record["item"] for record in evidence.evaluation}, {"q11"}, "the query text is never inspected")

    def test_every_arm_is_evaluation_only(self):
        evidence = ev.dataset_a_evidence(self.ROWS)
        self.assertEqual(evidence.observations, [])
        self.assertEqual(len(evidence.evaluation), 8, "q1 and q7 give three arms each, q6 two (kimi missing)")
        self.assertEqual(evidence.dropped["arm:kimi_correct-missing-or-invalid"], 1)
        self.assertEqual({record["model"] for record in evidence.evaluation}, {"qwen", "ds4", "kimi"})
        for record in evidence.evaluation:
            self.assertEqual(record["reason"], ev.REASON_NOT_SERVED)
            self.assertFalse(darwin_route_accepts(record))
        classes = {record["task"]: record["taskClass"] for record in evidence.evaluation}
        self.assertEqual(classes, {"gsm8k": "reasoning", "ifeval": "extraction", "gpqa_diamond": None})

    def test_cli_reads_the_gzip_file_and_has_no_observation_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sources, downloads, data = pinned(directory, ev.DATASET_A, gzip.compress(jsonl(self.ROWS), mtime=0), download={"rows": 8})
            evaluation_out = directory / "dataset-a-evaluation.jsonl"
            common = ["dataset-a-routing", "--sources", str(sources), "--downloads", str(downloads), "--file", str(data)]
            code, stdout, stderr = run_main([*common, "--evaluation-out", str(evaluation_out)])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["evaluation_records"], 8)
            manifest = json.loads(evaluation_out.with_suffix(".jsonl.manifest.json").read_text())
            self.assertEqual(manifest["source"]["licence"]["spdx"], "CC-BY-4.0")
            self.assertIn("CC-BY-4.0", manifest["source"]["attribution"])
            self.assertEqual(manifest["dropped"]["row:schema-anchor"], 1)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ev.main([*common, "--evaluation-out", str(evaluation_out), "--out", str(directory / "obs.jsonl")])
            self.assertFalse((directory / "obs.jsonl").exists())


class IclRouterTests(unittest.TestCase):
    ROWS = [
        icl_row(0, "Qwen2.5-7B-Instruct", "gsm8k", True),
        icl_row(1, "Qwen2.5-7B-Instruct", "mbpp", False),
        icl_row(2, "Llama-3.1-8B-Instruct", "gsm8k", True),
        icl_row(3, "meta-llama/Meta-Llama-3.1-70B-Instruct", "ifeval", True),
        icl_row(4, "Qwen2.5-7B-Instruct", "arc_challenge", True),
        icl_row(5, "glm-4-9b-chat", "gsm8k", True),
        icl_row(6, "Qwen2.5-7B-Instruct", "gsm8k", "yes"),
    ]

    def test_llama_rows_are_evaluation_only_even_when_a_provider_serves_that_model(self):
        evidence = ev.icl_router_evidence(self.ROWS, [QWEN_HOST, LLAMA_HOST])
        self.assertNotIn("router-llama-host", {row["providerId"] for row in evidence.observations})
        llama = [record for record in evidence.evaluation if record["reason"] == ev.REASON_LLAMA]
        self.assertEqual({record["item"] for record in llama}, {"2", "3"})
        self.assertTrue(all(not darwin_route_accepts(record) for record in llama))

    def test_other_rows_seed_only_providers_serving_that_exact_model(self):
        evidence = ev.icl_router_evidence(self.ROWS, [QWEN_HOST, GROQ, CLOUDFLARE])
        self.assertEqual([(row["providerId"], row["taskClass"], row["quality"]) for row in evidence.observations],
                         [("router-qwen-host", "reasoning", 1.0), ("router-qwen-host", "coding", 0.0)])
        for row in evidence.observations:
            self.assertIsNone(row["latencyMs"])
            self.assertIsNone(row["costUsd"], "ICL-Router records no token counts")
            self.assertTrue(darwin_route_accepts(row))
        reasons = {record["item"]: record["reason"] for record in evidence.evaluation}
        self.assertEqual(reasons, {"2": ev.REASON_LLAMA, "3": ev.REASON_LLAMA, "4": ev.REASON_UNMAPPED, "5": ev.REASON_NOT_SERVED})
        self.assertEqual(evidence.dropped["row:invalid-is_correct_direct"], 1)

    def test_cli_reads_a_json_array_and_refuses_an_empty_observation_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sources, downloads, data = pinned(directory, ev.ICL_ROUTER, json.dumps(self.ROWS).encode("utf-8"), download={"rows": 7})
            common = ["icl-router", "--sources", str(sources), "--downloads", str(downloads), "--file", str(data)]
            out, evaluation_out = directory / "obs.jsonl", directory / "eval.jsonl"
            code, _, stderr = run_main([*common, "--catalog", str(catalog_file(directory, [GROQ, LLAMA_HOST])),
                                        "--out", str(out), "--evaluation-out", str(evaluation_out)])
            self.assertEqual(code, 2, "no catalog provider serves a non-Llama ICL-Router model")
            self.assertIn("no route observations", stderr)
            self.assertFalse(out.exists() or evaluation_out.exists())
            code, stdout, stderr = run_main([*common, "--catalog", str(catalog_file(directory, [GROQ, LLAMA_HOST])),
                                             "--evaluation-out", str(evaluation_out)])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["evaluation_records"], 6)
            self.assertTrue(all(record["use"] == "evaluation-only" for record in read_jsonl(evaluation_out)))


class OutputTests(unittest.TestCase):
    def test_output_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "obs.jsonl"
            rows = [ev.observation("coding", {**GROQ, "inputPer1M": 0.15, "outputPer1M": 0.75}, 1.0, 10, 5, "test")]
            manifest = ev.write_output(out, rows, {"source": "test"})
            self.assertEqual(manifest["records"], 1)
            self.assertEqual(manifest["use"], "darwin-route-observations")
            self.assertEqual(manifest["cells"], {"coding/router-groq": 1})
            self.assertEqual(manifest["records_sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertEqual(json.loads(out.read_text().strip())["providerId"], "router-groq")
            self.assertTrue(out.with_suffix(".jsonl.manifest.json").exists())
            with self.assertRaises(ev.EvidenceError):
                ev.write_output(out, [], {})
            evaluation = [ev.evaluation_record("test", ev.REASON_LLAMA, 1, "gsm8k", "reasoning", "Llama-3.1-8B-Instruct", 1.0)]
            with self.assertRaises(ev.EvidenceError, msg="an evaluation record cannot go to the observation output"):
                ev.write_output(out, evaluation, {})
            with self.assertRaises(ev.EvidenceError, msg="an observation cannot go to the evaluation output"):
                ev.write_output(Path(tmp) / "eval.jsonl", rows, {}, ev.EVALUATION_ONLY)

    def test_observation_rejects_a_class_or_quality_darwin_route_cannot_take(self):
        with self.assertRaises(ev.EvidenceError):
            ev.observation("astrology", GROQ, 1.0, None, None, "test")
        with self.assertRaises(ev.EvidenceError):
            ev.observation("coding", GROQ, 1.5, None, None, "test")


if __name__ == "__main__":
    unittest.main()
