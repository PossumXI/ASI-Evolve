"""Permissively licensed routing evidence for Immaculate's Darwin-Route, as content-free route observations.

    python -m arobi_integrations.route_evidence eljefe-router-data --sources MANIFEST-SOURCES.json \
        --downloads DOWNLOADS.json --file scored_gptoss120b.jsonl --catalog provider_catalog.json \
        --out eljefe-observations.jsonl [--evaluation-out eljefe-evaluation.jsonl]
    python -m arobi_integrations.route_evidence icl-router --sources MANIFEST-SOURCES.json \
        --downloads DOWNLOADS.json --file train_router.json --catalog provider_catalog.json \
        [--out icl-observations.jsonl] [--evaluation-out icl-evaluation.jsonl]
    python -m arobi_integrations.route_evidence dataset-a-routing --sources MANIFEST-SOURCES.json \
        --downloads DOWNLOADS.json --file train.jsonl.gz --evaluation-out dataset-a-evaluation.jsonl

Darwin-Route (PossumXI/Immaculate apps/harness/src/darwin-route.ts) is the one route-policy search. It learns
per task class x provider from route observations {taskClass, providerId, ok, latencyMs, costUsd, quality}.
Live evidence comes from Immaculate's route-outcome sink and the darwin:route measure and shadow steps; that is
our own data, it is always allowed, and it goes to Darwin-Route directly without passing through here. This
module adds third-party evidence that exists before live traffic does. Feed its --out file to
`npm run darwin:route -- evolve --observations <file>`.

Licence rule (owner decision, 2026-09-28). Third-party routing data is used only under a permissive licence, by
SPDX id: MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, CC-BY-4.0, CC0-1.0 or Unlicense. The operator's source
manifest must record the licence from BOTH the dataset card and a licence file in the repository (not the card
itself); both must be on that list and they must agree. Anything else is refused. xRouteBench, RouterBench and
LLMRouterBench declare no licence on their cards: their adapters are gone, their subcommands only print the
refusal, and the gate refuses their repositories whatever a manifest claims.

Pinning. Nothing is fetched. The operator downloads each file and records, in a MANIFEST-SOURCES.json-style
file, the repository, commit and licence evidence, and in a DOWNLOADS.json-style file, the repository, commit,
file path and sha256 (optionally bytes and rows). A file is read only when its commit matches the revision the
adapter was written for and its sha256 matches the pin. ROUTE_EVIDENCE.md documents both shapes.

Task classes. The owner's mapping onto Darwin-Route's classes (coding, research, reasoning, extraction,
conversation):
    mbpp                        -> coding
    gsm8k, math500, mmlu_pro    -> reasoning
    ifeval                      -> extraction   (format-constrained instruction following)
Task names are compared lower-cased with punctuation removed (MMLU-Pro is mmlu_pro). Any other task has no class
and never becomes an observation. The manifest records the rule that decided every task seen.

Sources and their rules:
  eljefe-router-data  DJLougen/eljefe-router-data@3239a7df (Apache-2.0), scored_gptoss120b.jsonl
      Only the gpt-oss-120b (frontier_*) arm seeds observations, and only for router-groq, router-huggingface
      and router-cerebras, each only while the operator's catalog says it serves gpt-oss-120b.
      quality = frontier_score; ok = true; latencyMs = null (frontier_latency_ms was measured on Fireworks, not
      on these providers); costUsd = frontier_input_tokens and frontier_output_tokens x the provider's own per-1M
      prices from the catalog. frontier_cost (Fireworks' price) is never read. The local_* arm (gemma-4-E4B-it)
      goes to the evaluation output.
  dataset-a-routing   massaindustries/dataset-A-routing@48bfd5ce (CC-BY-4.0), data/results/train.jsonl.gz
      Evaluation reference only: none of our fallbacks serves its models (the qwen, ds4 and kimi arms), so it
      takes no --out. The _schema_anchor row, and every AIME and LiveCodeBench row (upstream copyright), are
      dropped from all output.
  icl-router          lalalamdbf/ICL-Router@6f2aa6eb (Apache-2.0), train_router.json
      Llama rows are evaluation-only: the rule names Llama-3.1, whose licence carries a naming clause for
      anything built from its outputs, and the gate covers every Meta Llama release. Any other row becomes an
      observation only for catalog providers serving that exact model: quality = is_correct_direct, ok = true,
      latencyMs and costUsd null (neither latency nor token counts are recorded).

Evaluation output (--evaluation-out) holds rows that must never be route observations for a serving provider.
Every line says "use": "evaluation-only" with a reason and carries no providerId or ok field, so Darwin-Route's
observation loader rejects it if it is ever passed as --observations. Both outputs are content-free: no query,
prompt or response text is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TASK_CLASSES = ("coding", "research", "reasoning", "extraction", "conversation")

# Owner decision (2026-09-28): benchmark task, normalised by normalise_task, -> Darwin-Route task class.
OWNER_CLASS_MAP = {
    "mbpp": "coding",
    "gsm8k": "reasoning",
    "math500": "reasoning",
    "mmlupro": "reasoning",
    "ifeval": "extraction",
}

PERMISSIVE_LICENCES = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC-BY-4.0", "CC0-1.0", "Unlicense")
LICENCE_RULE = (
    "third-party routing data may be used only under a permissive licence (MIT, Apache-2.0, BSD-2-Clause, "
    "BSD-3-Clause, CC-BY-4.0, CC0-1.0 or Unlicense), verified from both the dataset card and a licence file in "
    "the repository (owner decision 2026-09-28)"
)

# Command name -> (display name, repository). Refused in code; no manifest entry or flag lifts this.
UNLICENSED_SOURCES = {
    "xroutebench": ("xRouteBench", "ulab-ai/xRouteBench"),
    "routerbench": ("RouterBench", "withmartian/routerbench"),
    "llmrouterbench": ("LLMRouterBench", "NPULH/LLMRouterBench"),
}

GPT_OSS_120B = "gpt-oss-120b"
GPT_OSS_120B_PROVIDERS = ("router-groq", "router-huggingface", "router-cerebras")
DATASET_A_ARMS = (("qwen", "qwen_correct"), ("ds4", "ds4_correct"), ("kimi", "kimi_correct"))

OBSERVATIONS = "darwin-route-observations"
EVALUATION_ONLY = "evaluation-only"
MANIFEST_SCHEMA = "arobi.route-evidence.v2"
EVALUATION_SCHEMA = "arobi.route-evaluation.v1"

REASON_LLAMA = "llama-licence-naming-clause"
REASON_NOT_SERVED = "model-not-served-by-our-fallbacks"
REASON_UNMAPPED = "task-class-unmapped"

_HEX = re.compile(r"^[0-9a-f]+$")
_META_LLAMA = re.compile(r"(?<![a-z])llama[-_ ]?\d")


class EvidenceError(ValueError):
    """Input that cannot become route evidence."""


class LicenceError(EvidenceError):
    """A source the licence rule refuses."""


class PinError(EvidenceError):
    """A file or manifest that does not match the pinned commit and sha256."""


@dataclass(frozen=True)
class SourceSpec:
    """One third-party file this module reads, pinned to the revision its adapter was written for."""

    key: str
    repo: str
    commit: str
    path: str
    licence: str

    @property
    def label(self) -> str:
        return f"{self.repo}@{self.commit}:{self.path}"


ELJEFE = SourceSpec("eljefe-router-data", "DJLougen/eljefe-router-data", "3239a7df", "scored_gptoss120b.jsonl", "Apache-2.0")
DATASET_A = SourceSpec("dataset-a-routing", "massaindustries/dataset-A-routing", "48bfd5ce", "data/results/train.jsonl.gz", "CC-BY-4.0")
ICL_ROUTER = SourceSpec("icl-router", "lalalamdbf/ICL-Router", "6f2aa6eb", "train_router.json", "Apache-2.0")


# ---------------------------------------------------------------------------------------------- classification


def normalise_task(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def classify_task(task_name: object) -> tuple[str | None, str]:
    """(task class, deciding rule) under the owner's mapping; (None, reason) for a task it does not name."""
    key = normalise_task(task_name)
    task_class = OWNER_CLASS_MAP.get(key)
    if task_class is None:
        return None, "unmapped: not in the owner's class map"
    return task_class, f"owner-map:{key}->{task_class}"


def canonical_model(name: object) -> str:
    """'openai/gpt-oss-120b:fastest' and 'accounts/fireworks/models/gpt-oss-120b' are both 'gpt-oss-120b'."""
    text = str(name or "").strip().lower()
    return text.rsplit("/", 1)[-1].split(":", 1)[0]


def is_meta_llama(model: object) -> bool:
    lowered = str(model or "").strip().lower()
    return lowered.startswith("meta-llama/") or bool(_META_LLAMA.search(lowered))


class _TaskClasses:
    """Classifies tasks and remembers the rule that decided each one, for the manifest."""

    def __init__(self) -> None:
        self.decided: dict[str, dict] = {}

    def __call__(self, task_name: object) -> str | None:
        key = normalise_task(task_name) or "(none)"
        if key not in self.decided:
            task_class, rule = classify_task(task_name)
            self.decided[key] = {"taskClass": task_class, "rule": rule}
        return self.decided[key]["taskClass"]


# ------------------------------------------------------------------------------------------------ licence gate


def spdx_permissive(value: object) -> str | None:
    """The allowlisted SPDX id this value names (case-insensitive), or None."""
    text = str(value or "").strip().lower()
    for licence in PERMISSIVE_LICENCES:
        if text == licence.lower():
            return licence
    return None


def unlicensed_refusal(command: str) -> str:
    name, repo = UNLICENSED_SOURCES[command]
    return (
        f"{name} ({repo}) is refused: its dataset card declares no licence, and {LICENCE_RULE}. Its adapter was "
        "removed; the refusal is in code and no manifest entry or flag lifts it."
    )


def check_licence(entry: dict) -> dict:
    """Refuse a source-manifest entry unless the card and a repository licence file name the same allowed licence."""
    repo = str(entry.get("repo", "")).strip()
    if not repo:
        raise LicenceError(f"a source manifest entry has no repo; {LICENCE_RULE}")
    for command, (_, blocked) in UNLICENSED_SOURCES.items():
        if repo.lower() == blocked.lower():
            raise LicenceError(unlicensed_refusal(command))
    licence = entry.get("license")
    if not isinstance(licence, dict):
        raise LicenceError(f"{repo}: the source manifest records no licence evidence (license.card, license.repoFile); {LICENCE_RULE}")
    card = licence.get("card")
    card_id = spdx_permissive(card)
    if card_id is None:
        raise LicenceError(f"{repo}: the card licence {card!r} is not an allowed SPDX id; {LICENCE_RULE}")
    repo_file = licence.get("repoFile")
    file_path = str(repo_file.get("path", "")).strip() if isinstance(repo_file, dict) else ""
    if not file_path:
        raise LicenceError(f"{repo}: no repository licence file is recorded (license.repoFile.path and .spdx); {LICENCE_RULE}")
    if re.split(r"[\\/]", file_path)[-1].lower() in {"readme", "readme.md"}:
        raise LicenceError(f"{repo}: {file_path} is the dataset card, not a separate licence file; {LICENCE_RULE}")
    file_id = spdx_permissive(repo_file.get("spdx"))
    if file_id is None:
        raise LicenceError(f"{repo}: {file_path} records licence {repo_file.get('spdx')!r}, not an allowed SPDX id; {LICENCE_RULE}")
    if file_id != card_id:
        raise LicenceError(f"{repo}: the card says {card_id} but {file_path} says {file_id}; an ambiguous licence is refused")
    evidence = {"path": file_path, "spdx": file_id}
    if repo_file.get("sha256"):
        evidence["sha256"] = str(repo_file["sha256"]).strip().lower()
    return {"spdx": card_id, "card": str(card).strip(), "repoFile": evidence}


# ----------------------------------------------------------------------------------------------------- pinning


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest_entries(path: Path, key: str) -> list[dict]:
    """The entries of a MANIFEST-SOURCES.json ('sources') or DOWNLOADS.json ('downloads') style file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PinError(f"{path} does not exist") from error
    except json.JSONDecodeError as error:
        raise PinError(f"{path}: not JSON ({error})") from error
    entries = raw.get(key) if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise PinError(f"{path}: expected an object with a {key!r} list of objects (or that list itself)")
    return entries


def _repo_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _one(entries: list[dict], where: str, repo: str, path: str | None = None) -> dict:
    found = [
        entry for entry in entries
        if str(entry.get("repo", "")).strip().lower() == repo.lower()
        and (path is None or _repo_path(entry.get("path")) == path)
    ]
    what = repo if path is None else f"{repo} {path}"
    if not found:
        raise PinError(f"{where} has no entry for {what}")
    if len(found) > 1:
        raise PinError(f"{where} has {len(found)} entries for {what}; pin exactly one")
    return found[0]


def _commit(value: object, where: str) -> str:
    text = str(value or "").strip().lower()
    if not 7 <= len(text) <= 40 or not _HEX.match(text):
        raise PinError(f"{where}: {value!r} is not a commit id (7 to 40 hex characters)")
    return text


def verify_source(spec: SourceSpec, sources: list[dict], downloads: list[dict], file_path: Path) -> dict:
    """Licence gate first, then commit and sha256 pins. Returns the provenance recorded in every output manifest."""
    entry = _one(sources, "the source manifest", spec.repo)
    licence = check_licence(entry)
    if licence["spdx"] != spec.licence:
        raise LicenceError(
            f"{spec.repo}: the manifest records {licence['spdx']}, but this adapter was written for the "
            f"{spec.licence} release at {spec.commit}; re-verify the source before changing the adapter"
        )
    commit = _commit(entry.get("commit"), f"{spec.repo} in the source manifest")
    if not commit.startswith(spec.commit):
        raise PinError(f"{spec.repo}: the source manifest pins commit {commit}, but this adapter reads revision {spec.commit}")
    download = _one(downloads, "the downloads manifest", spec.repo, spec.path)
    download_commit = _commit(download.get("commit"), f"{spec.repo} {spec.path} in the downloads manifest")
    if not (download_commit.startswith(commit) or commit.startswith(download_commit)):
        raise PinError(f"{spec.repo}: {spec.path} was downloaded at {download_commit}, not at the manifest's commit {commit}")
    expected = str(download.get("sha256", "")).strip().lower()
    if len(expected) != 64 or not _HEX.match(expected):
        raise PinError(f"{spec.repo} {spec.path}: the downloads manifest needs a 64-hex sha256")
    if not file_path.is_file():
        raise PinError(f"{file_path} does not exist")
    size = file_path.stat().st_size
    expected_bytes = download.get("bytes")
    if expected_bytes is not None and (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes != size):
        raise PinError(f"refusing {file_path}: {size} bytes, but the downloads manifest pins {expected_bytes!r}")
    actual = sha256_file(file_path)
    if actual != expected:
        raise PinError(f"refusing {file_path}: sha256 {actual} does not match the pinned {expected} for {spec.label}")
    expected_rows = download.get("rows")
    if expected_rows is not None and (isinstance(expected_rows, bool) or not isinstance(expected_rows, int) or expected_rows < 0):
        raise PinError(f"{spec.repo} {spec.path}: the downloads manifest's rows must be a non-negative integer")
    pinned_commit = max(commit, download_commit, key=len)
    return {
        "repo": spec.repo,
        "commit": pinned_commit,
        "path": spec.path,
        "sha256": actual,
        "bytes": size,
        "expected_rows": expected_rows,
        "licence": licence,
        "attribution": f"{spec.repo} at {pinned_commit}, licensed {licence['spdx']}",
    }


# -------------------------------------------------------------------------------------------- inputs and rows


def load_catalog(path: Path) -> list[dict]:
    """{"providers": [{providerId, servedModel, inputPer1M, outputPer1M}]}: the operator's own price table."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{path}: cannot read the catalog ({error})") from error
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, list) or not providers:
        raise EvidenceError("the catalog needs a non-empty 'providers' list")
    catalog, seen = [], set()
    for entry in providers:
        if not isinstance(entry, dict):
            raise EvidenceError("every catalog provider must be an object")
        provider_id = str(entry.get("providerId", "")).strip()
        if not provider_id or provider_id in seen:
            raise EvidenceError(f"catalog provider ids must be unique and non-empty (got {provider_id!r})")
        seen.add(provider_id)
        try:
            input_price, output_price = float(entry["inputPer1M"]), float(entry["outputPer1M"])
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceError(f"{provider_id}: inputPer1M and outputPer1M are required (0 for a free tier)") from error
        if not (math.isfinite(input_price) and math.isfinite(output_price)) or input_price < 0 or output_price < 0:
            raise EvidenceError(f"{provider_id}: prices must be finite and not negative")
        served = str(entry.get("servedModel", "") or "").strip()
        catalog.append({"providerId": provider_id, "servedModel": served or None, "inputPer1M": input_price, "outputPer1M": output_price})
    return catalog


def read_rows(path: Path) -> list[dict]:
    """JSON lines or a JSON array of objects; gzip when the name ends in .gz."""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8-sig") as handle:
                text = handle.read()
        else:
            text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise EvidenceError(f"{path}: cannot read ({error})") from error
    if text.lstrip().startswith("["):
        try:
            rows = json.loads(text)
        except json.JSONDecodeError as error:
            raise EvidenceError(f"{path}: not JSON ({error})") from error
    else:
        rows = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise EvidenceError(f"{path}:{number}: not JSON ({error})") from error
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise EvidenceError(f"{path}: expected JSON objects, one per line or in one array")
    return rows


def _score(value: object) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return None


def _tokens(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < 0 or value != int(value):
        return None
    return int(value)


def _item(value: object) -> str | None:
    return None if value is None else str(value)


# ------------------------------------------------------------------------------------------ records and adapters


def observation(task_class: str, provider: dict, quality: float, input_tokens: int | None, output_tokens: int | None, source: str) -> dict:
    """One Darwin-Route observation. latencyMs is always null: no source here measured our providers' latency."""
    if task_class not in TASK_CLASSES:
        raise EvidenceError(f"{task_class!r} is not a Darwin-Route task class")
    if not 0 <= quality <= 1:
        raise EvidenceError(f"quality {quality!r} is outside 0..1")
    cost = None
    if input_tokens is not None and output_tokens is not None:
        cost = input_tokens * provider["inputPer1M"] / 1e6 + output_tokens * provider["outputPer1M"] / 1e6
    return {
        "taskClass": task_class,
        "providerId": provider["providerId"],
        "ok": True,
        "latencyMs": None,
        "costUsd": cost,
        "quality": quality,
        "source": source,
    }


def evaluation_record(source: str, reason: str, item: object, task: object, task_class: str | None, model: object, quality: float) -> dict:
    """A row that must never be a route observation. It has no providerId or ok, so Darwin-Route rejects it."""
    return {
        "schema": EVALUATION_SCHEMA,
        "use": EVALUATION_ONLY,
        "reason": reason,
        "source": source,
        "item": _item(item),
        "task": None if task is None else str(task),
        "taskClass": task_class,
        "model": None if model is None else str(model),
        "quality": quality,
    }


@dataclass
class Evidence:
    observations: list[dict] = field(default_factory=list)
    evaluation: list[dict] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)
    counts: Counter = field(default_factory=Counter)
    task_classes: dict[str, dict] = field(default_factory=dict)
    rules: dict[str, str] = field(default_factory=dict)


def gpt_oss_120b_providers(catalog: list[dict]) -> list[dict]:
    return [
        provider for provider in catalog
        if provider["providerId"] in GPT_OSS_120B_PROVIDERS and canonical_model(provider["servedModel"]) == GPT_OSS_120B
    ]


def eljefe_evidence(rows: list[dict], catalog: list[dict], source: str = ELJEFE.label) -> Evidence:
    providers = gpt_oss_120b_providers(catalog)
    if not providers:
        raise EvidenceError(
            f"no catalog provider among {', '.join(GPT_OSS_120B_PROVIDERS)} serves {GPT_OSS_120B} "
            "(servedModel), so this source can seed nothing"
        )
    classes = _TaskClasses()
    evidence = Evidence(rules={
        "providers": f"{', '.join(GPT_OSS_120B_PROVIDERS)}, each only while the catalog says it serves {GPT_OSS_120B}",
        "quality": "frontier_score: the gpt-oss-120b arm's graded score (0..1)",
        "ok": "true: the item was answered; a wrong answer is low quality, not a failed call",
        "latencyMs": "null: frontier_latency_ms was measured on Fireworks, not on these providers",
        "costUsd": "frontier_input_tokens and frontier_output_tokens x the provider's own per-1M prices from the catalog; frontier_cost (Fireworks) is never read",
        "evaluation": "the local_* arm (gemma-4-E4B-it), which no fallback serves; gpt-oss-120b rows whose task has no class",
    })
    for row in rows:
        task = row.get("source")
        task_class = classes(task)
        item = row.get("id")
        frontier_model = row.get("frontier_model")
        score = _score(row.get("frontier_score"))
        if score is None:
            evidence.dropped["row:invalid-frontier_score"] += 1
        elif canonical_model(frontier_model) != GPT_OSS_120B:
            evidence.evaluation.append(evaluation_record(source, REASON_NOT_SERVED, item, task, task_class, frontier_model, score))
        elif task_class is None:
            evidence.evaluation.append(evaluation_record(source, REASON_UNMAPPED, item, task, None, frontier_model, score))
        else:
            input_tokens = _tokens(row.get("frontier_input_tokens"))
            output_tokens = _tokens(row.get("frontier_output_tokens"))
            for provider in providers:
                evidence.observations.append(observation(task_class, provider, score, input_tokens, output_tokens, source))
        if "local_model" in row or "local_score" in row:
            local_score = _score(row.get("local_score"))
            if local_score is None:
                evidence.dropped["arm:invalid-local_score"] += 1
            else:
                evidence.evaluation.append(
                    evaluation_record(source, REASON_NOT_SERVED, item, task, task_class, row.get("local_model"), local_score)
                )
    evidence.task_classes = dict(sorted(classes.decided.items()))
    return evidence


def is_schema_anchor(row: dict) -> bool:
    return "_schema_anchor" in row or any(
        isinstance(value, str) and value.strip().startswith("_schema_anchor") for value in row.values()
    )


def upstream_copyright(row: dict) -> str | None:
    """'aime' or 'livecodebench' when the row comes from a source whose problems are under upstream copyright."""
    for key in ("source", "evaluation_protocol_id"):
        tokens = [token for token in re.split(r"[^a-z0-9]+", str(row.get(key) or "").lower()) if token]
        if any(token.startswith("aime") for token in tokens):
            return "aime"
        if "livecodebench" in "".join(tokens) or "lcb" in tokens:
            return "livecodebench"
    return None


def dataset_a_evidence(rows: list[dict], source: str = DATASET_A.label) -> Evidence:
    classes = _TaskClasses()
    evidence = Evidence(rules={
        "evaluation": "every arm (qwen, ds4, kimi): none of our fallbacks serves these models",
        "quality": "the arm's *_correct flag as 0 or 1",
        "dropped": "the _schema_anchor row, and AIME and LiveCodeBench rows (upstream copyright; the rule names AIME-2025 and every AIME year carries the same copyright)",
    })
    for row in rows:
        if is_schema_anchor(row):
            evidence.dropped["row:schema-anchor"] += 1
            continue
        protected = upstream_copyright(row)
        if protected:
            evidence.dropped[f"row:upstream-copyright:{protected}"] += 1
            continue
        if row.get("gated") is True:
            evidence.counts["gated-rows"] += 1
        task = row.get("source")
        task_class = classes(task)
        for model, column in DATASET_A_ARMS:
            score = _score(row.get(column))
            if score is None:
                evidence.dropped[f"arm:{column}-missing-or-invalid"] += 1
                continue
            evidence.evaluation.append(evaluation_record(source, REASON_NOT_SERVED, row.get("query_id"), task, task_class, model, score))
    evidence.task_classes = dict(sorted(classes.decided.items()))
    return evidence


def icl_router_evidence(rows: list[dict], catalog: list[dict], source: str = ICL_ROUTER.label) -> Evidence:
    by_model: dict[str, list[dict]] = {}
    for provider in catalog:
        if provider["servedModel"]:
            by_model.setdefault(canonical_model(provider["servedModel"]), []).append(provider)
    classes = _TaskClasses()
    evidence = Evidence(rules={
        "providers": "catalog providers whose servedModel is exactly the row's model; never for a Llama row",
        "quality": "is_correct_direct as 0 or 1",
        "ok": "true: the item was answered; a wrong answer is low quality, not a failed call",
        "latencyMs": "null: not recorded",
        "costUsd": "null: no token counts are recorded",
        "evaluation": "Llama rows (licence naming clause), models no catalog provider serves, and tasks with no class",
    })
    for row in rows:
        model = row.get("model")
        task = row.get("task")
        task_class = classes(task)
        item = row.get("index")
        score = _score(row.get("is_correct_direct"))
        if score is None:
            evidence.dropped["row:invalid-is_correct_direct"] += 1
            continue
        if is_meta_llama(model):
            evidence.evaluation.append(evaluation_record(source, REASON_LLAMA, item, task, task_class, model, score))
            continue
        providers = by_model.get(canonical_model(model), [])
        if not providers:
            evidence.evaluation.append(evaluation_record(source, REASON_NOT_SERVED, item, task, task_class, model, score))
        elif task_class is None:
            evidence.evaluation.append(evaluation_record(source, REASON_UNMAPPED, item, task, None, model, score))
        else:
            for provider in providers:
                evidence.observations.append(observation(task_class, provider, score, None, None, source))
    evidence.task_classes = dict(sorted(classes.decided.items()))
    return evidence


# ------------------------------------------------------------------------------------------------------ output


def write_output(out: Path, records: list[dict], manifest: dict, use: str = OBSERVATIONS) -> dict:
    """Write records as JSON lines and <out>.manifest.json describing them."""
    if use not in (OBSERVATIONS, EVALUATION_ONLY):
        raise EvidenceError(f"unknown output use {use!r}")
    if not records:
        raise EvidenceError(f"no {use} records; nothing to write to {out}")
    if use == OBSERVATIONS and any("providerId" not in record for record in records):
        raise EvidenceError("an observation output may hold only route observations")
    if use == EVALUATION_ONLY and any(record.get("use") != EVALUATION_ONLY or "providerId" in record for record in records):
        raise EvidenceError("an evaluation output may hold only evaluation-only records")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    if use == OBSERVATIONS:
        cells = Counter(f"{record['taskClass']}/{record['providerId']}" for record in records)
    else:
        cells = Counter(f"{record['taskClass'] or 'unclassified'}/{record['model']}" for record in records)
    manifest = dict(manifest)
    manifest.update(
        {
            "schema": MANIFEST_SCHEMA,
            "use": use,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "records": len(records),
            "records_sha256": sha256_file(out),
            "cells": dict(sorted(cells.items())),
        }
    )
    if use == EVALUATION_ONLY:
        manifest["reasons"] = dict(sorted(Counter(record["reason"] for record in records).items()))
    manifest_path = out.with_suffix(out.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def run_source(spec: SourceSpec, args: argparse.Namespace) -> dict:
    sources = load_manifest_entries(args.sources, "sources")
    downloads = load_manifest_entries(args.downloads, "downloads")
    provenance = verify_source(spec, sources, downloads, args.file)
    rows = read_rows(args.file)
    if provenance["expected_rows"] is not None and provenance["expected_rows"] != len(rows):
        raise PinError(f"{args.file}: {len(rows)} records, but the downloads manifest pins {provenance['expected_rows']}")
    catalog_path = getattr(args, "catalog", None)
    catalog = load_catalog(catalog_path) if catalog_path else []
    if spec is ELJEFE:
        evidence = eljefe_evidence(rows, catalog)
    elif spec is ICL_ROUTER:
        evidence = icl_router_evidence(rows, catalog)
    else:
        evidence = dataset_a_evidence(rows)
    out = getattr(args, "out", None)
    evaluation_out = getattr(args, "evaluation_out", None)
    if not out and not evaluation_out:
        raise EvidenceError("pass --out and/or --evaluation-out")
    if out and evaluation_out and out.resolve() == evaluation_out.resolve():
        raise EvidenceError("--out and --evaluation-out must be different files")
    if out and not evidence.observations:
        raise EvidenceError(f"{spec.repo} yields no route observations for this catalog; nothing written")
    if evaluation_out and not evidence.evaluation:
        raise EvidenceError(f"{spec.repo} yields no evaluation-only records; nothing written")
    manifest = {
        "source": provenance,
        "licence_rule": LICENCE_RULE,
        "sources_manifest_sha256": sha256_file(args.sources),
        "downloads_manifest_sha256": sha256_file(args.downloads),
        "catalog_sha256": sha256_file(catalog_path) if catalog_path else None,
        "rows_read": len(rows),
        "dropped": dict(sorted(evidence.dropped.items())),
        "counts": dict(sorted(evidence.counts.items())),
        "task_classes": evidence.task_classes,
        "rules": evidence.rules,
    }
    summary: dict = {"source": spec.label, "rows_read": len(rows), "dropped": manifest["dropped"]}
    if out:
        summary["out"] = str(out)
        summary["observations"] = write_output(out, evidence.observations, manifest, OBSERVATIONS)["records"]
    if evaluation_out:
        summary["evaluation_out"] = str(evaluation_out)
        summary["evaluation_records"] = write_output(evaluation_out, evidence.evaluation, manifest, EVALUATION_ONLY)["records"]
    elif evidence.evaluation:
        summary["evaluation_records_not_written"] = len(evidence.evaluation)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m arobi_integrations.route_evidence",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="xroutebench, routerbench and llmrouterbench are refused: their dataset cards declare no licence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for spec in (ELJEFE, ICL_ROUTER, DATASET_A):
        command = sub.add_parser(spec.key, help=f"{spec.repo}@{spec.commit} ({spec.licence}): {spec.path}")
        command.add_argument("--sources", type=Path, required=True, help="MANIFEST-SOURCES.json: commit and licence evidence per repository")
        command.add_argument("--downloads", type=Path, required=True, help="DOWNLOADS.json: commit and sha256 per downloaded file")
        command.add_argument("--file", type=Path, required=True, help=f"the local copy of {spec.path}")
        if spec is not DATASET_A:
            command.add_argument("--catalog", type=Path, required=True, help="the operator's provider price table")
            command.add_argument("--out", type=Path, required=spec is ELJEFE, help="Darwin-Route observations (JSON lines)")
        command.add_argument("--evaluation-out", type=Path, required=spec is DATASET_A, help="evaluation-only records (JSON lines)")
        command.set_defaults(spec=spec)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].strip().lower() in UNLICENSED_SOURCES:
        print(f"refused: {unlicensed_refusal(argv[0].strip().lower())}", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        summary = run_source(args.spec, args)
    except EvidenceError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
