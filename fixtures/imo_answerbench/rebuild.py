"""Reproduce the fixed four-problem fixture from a hash-verified upstream CSV.

Preparation only: this file is not part of the solver's runtime or tool surface.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMIT = "80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1"
SOURCE_PATH = "imobench/answerbench_v2.csv"
SOURCE_URL = f"https://raw.githubusercontent.com/google-deepmind/superhuman/{COMMIT}/{SOURCE_PATH}"
SOURCE_SHA256 = "275877a9d988d85278fad3a5f8a41d7f83393a60bf259531ec0a5161e6b21cf9"
SEED = "mathagent-imo-answerbench-four-v1"
CATEGORIES = {
    "algebra": "Algebra",
    "combinatorics": "Combinatorics",
    "geometry": "Geometry",
    "number_theory": "Number theory",
}
EXPECTED_IDS = [
    "imo-bench-algebra-004",
    "imo-bench-combinatorics-037",
    "imo-bench-geometry-055",
    "imo-bench-number_theory-081",
]
ANSWER_TYPES = ["symbolic_exponential", "integer", "integer", "empty_solution_set"]
HEADER = ["Problem ID", "Problem", "Short Answer", "Category", "Subcategory", "Source"]


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def selection_digest(problem_id: str) -> str:
    return digest(f"{SEED}|{problem_id}".encode("utf-8"))


def encoded_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build(raw: bytes) -> dict[str, bytes]:
    if digest(raw) != SOURCE_SHA256:
        raise ValueError("Upstream CSV hash mismatch; refusing to change the fixed experiment")
    parsed = list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))
    if parsed[0] != HEADER or len(parsed) != 401:
        raise ValueError("Unexpected upstream header or row count")
    rows = parsed[1:]
    ids = [row[0] for row in rows]
    if len(set(ids)) != 400:
        raise ValueError("Upstream Problem IDs are not unique")

    pools: dict[str, list[list[str]]] = {category: [] for category in CATEGORIES.values()}
    anomalies = []
    for row in rows:
        problem_id = row[0]
        family = problem_id.removeprefix("imo-bench-").rsplit("-", 1)[0]
        category = CATEGORIES[family]
        if len(row) != 6:
            if problem_id != "imo-bench-algebra-036" or len(row) != 5:
                raise ValueError(f"Unexpected malformed record: {problem_id}")
            # Keep its ID in the category pool so every official category has 100 IDs.
            # This record is not selected; no answer or statement is repaired here.
            if row[-3] != category:
                raise ValueError("Known malformed row no longer has its expected category")
            anomalies.append({
                "id": problem_id,
                "parsed_field_count": len(row),
                "expected_field_count": 6,
                "category": category,
                "handling": "Kept ID in category pool using ID family and trailing category; "
                "not selected; no statement or answer repair.",
                "affects_selected_ids": False,
            })
        elif row[3] != category:
            raise ValueError(f"Category and ID family disagree: {problem_id}")
        pools[category].append(row)

    if any(len(pool) != 100 for pool in pools.values()):
        raise ValueError("Expected 100 IDs in each category")
    selected = [
        min(pool, key=lambda row: (selection_digest(row[0]), row[0]))
        for pool in pools.values()
    ]
    if [row[0] for row in selected] != EXPECTED_IDS:
        raise ValueError("Deterministic selection no longer matches the frozen IDs")
    if any(len(row) != 6 for row in selected):
        raise ValueError("A selected record is malformed; do not replace it")

    problems = {
        "schema_version": "1.0",
        "benchmark": "IMO-AnswerBench",
        "problems": [
            {"id": row[0], "category": row[3], "problem": row[1]} for row in selected
        ],
    }
    answers = {
        "schema_version": "1.0",
        "benchmark": "IMO-AnswerBench",
        "answers": [
            {"id": row[0], "short_answer": row[2], "answer_type": answer_type}
            for row, answer_type in zip(selected, ANSWER_TYPES, strict=True)
        ],
    }
    outputs = {"problem-only.json": encoded_json(problems), "answer-key.json": encoded_json(answers)}
    manifest = {
        "schema_version": "1.0",
        "benchmark": "IMO-AnswerBench",
        "retrieved_on": "2026-09-08",
        "repository": "https://github.com/google-deepmind/superhuman",
        "source_commit": COMMIT,
        "source_path": SOURCE_PATH,
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "license": "CC-BY-4.0",
        "copyright": "Copyright 2025 Google LLC",
        "paper": "https://aclanthology.org/2025.emnlp-main.1794/",
        "columns": HEADER,
        "official_problem_count": len(rows),
        "category_counts_by_id_family": {category: len(pool) for category, pool in pools.items()},
        "well_formed_category_counts": dict(Counter(row[3] for row in rows if len(row) == 6)),
        "selection_rule_file": "selection-rule.json",
        "selection_seed": SEED,
        "source_anomalies": anomalies,
        "selected": [
            {
                "id": row[0],
                "category": row[3],
                "subcategory": row[4],
                "original_source": row[5],
                "selection_sha256": selection_digest(row[0]),
                "problem_sha256": digest(row[1].encode("utf-8")),
                "source_field_count": len(row),
            }
            for row in selected
        ],
        "artifact_sha256": {name: digest(content) for name, content in outputs.items()},
        "transformations": [
            "Selected one ID per official category using the predeclared SHA-256 rule.",
            "Decoded UTF-8 CSV and serialized selected fields as UTF-8 JSON.",
            "Retained selected Problem and Short Answer strings verbatim after CSV decoding.",
            "Added local schema, metadata and answer form labels; no mathematical edits.",
            "Separated problem statements from answer key and source metadata.",
        ],
    }
    outputs["manifest.json"] = encoded_json(manifest)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="Existing official CSV; offline verification")
    source.add_argument("--fetch", action="store_true", help="Download exact pinned official CSV")
    parser.add_argument("--check", action="store_true", help="Verify fixture bytes without writing")
    args = parser.parse_args()
    if args.source:
        raw = args.source.read_bytes()
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=30) as response:
            raw = response.read()
    for name, content in build(raw).items():
        target = ROOT / name
        if args.check:
            if target.read_bytes() != content:
                raise ValueError(f"Fixture mismatch: {name}")
        else:
            target.write_bytes(content)
    print("Verified fixed IDs: " + ", ".join(EXPECTED_IDS))


if __name__ == "__main__":
    main()
