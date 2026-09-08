# Fixed IMO-AnswerBench four-problem fixture

This is a reproducible four-problem integration sample of Google DeepMind's
IMO-AnswerBench. It is not an estimate of full-benchmark accuracy.

## Official source and attribution

- Dataset: [Google DeepMind Superhuman Reasoning / IMO Bench](https://github.com/google-deepmind/superhuman/tree/80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1/imobench).
- Pinned commit: `80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1`.
- File: [`imobench/answerbench_v2.csv`](https://raw.githubusercontent.com/google-deepmind/superhuman/80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1/imobench/answerbench_v2.csv).
- SHA-256: `275877a9d988d85278fad3a5f8a41d7f83393a60bf259531ec0a5161e6b21cf9`.
- Retrieved: 2026-09-08.
- Copyright 2025 Google LLC. Dataset and other non-software materials:
  [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
  The full license is saved in [CC-BY-4.0.txt](CC-BY-4.0.txt).
  Upstream software is separately licensed under Apache 2.0.
- Authors: Thang Luong, Dawsen Hwang, Hoang H. Nguyen, Golnaz Ghiasi,
  Yuri Chervonyi, Insuk Seo, Junsu Kim, Garrett Bingham, Jonathan Lee,
  Swaroop Mishra, Alex Zhai, Clara Huiyi Hu, Henryk Michalewski, Jimin Kim,
  Jeonghyun Ahn, Junhwi Bae, Xingyou Song, Trieu H. Trinh, Quoc V. Le,
  and Junehyuk Jung.
- Paper: [Towards Robust Mathematical Reasoning, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.1794/).

The upstream README designates `answerbench_v2.csv` as the current AnswerBench
version after corrections on 2026-02-12; `answerbench.csv` is deprecated. This
fixture selects records and serializes them to JSON; selected problem and answer
strings are unchanged after CSV decoding. Local schema fields, selection
metadata, and answer-form labels were added. No affiliation or endorsement is
implied. The upstream repository states that it is not an official Google product.

## Selection, fixed before solving

[selection-rule.json](selection-rule.json) records the rule. In each of the four
official categories, compute SHA-256 of UTF-8
`mathagent-imo-answerbench-four-v1|<Problem ID>` and take the ID with the smallest
hexadecimal digest (ID breaks a hypothetical tie). This uses neither the answer,
the problem text, difficulty, nor any solver result. Do not replace cases after
failures, inspection, or scoring.

| Problem ID | Official category | Subcategory |
| --- | --- | --- |
| `imo-bench-algebra-004` | Algebra | Inequality |
| `imo-bench-combinatorics-037` | Combinatorics | Extremal Combinatorics |
| `imo-bench-geometry-055` | Geometry | computation |
| `imo-bench-number_theory-081` | Number theory | Binomial |

The official benchmark contains 400 short-answer problems, 100 in each domain.
It includes both numerical and non-numerical answers. The paper describes
pre-IMO through IMO-Hard difficulty levels, but the published CSV does not
contain per-problem difficulty labels; this fixture makes no difficulty claims.

There is one upstream CSV format defect in this pinned file:
`imo-bench-algebra-036` decodes into five fields rather than six. Its ID family
and trailing category both identify Algebra, so its ID remains in the 100-ID
Algebra selection pool. It is not selected; including or excluding it gives the
same selected minimum. No repair to any problem or answer was made. All four
selected rows contain six complete fields. This defect is recorded in the
manifest rather than silently dropped.

## File separation and reproducibility

- [problem-only.json](problem-only.json): the only fixture the solve runner
  should open. Each case contains `id`, `category`, and `problem`.
- [answer-key.json](answer-key.json): independent post-run grading input;
  contains official `short_answer` strings and local answer-form labels.
- [manifest.json](manifest.json): pinned origin, selection hashes, source
  metadata, upstream defect, transformations, and hashes of the two JSON files.
- [rebuild.py](rebuild.py): preparation tool that verifies the source SHA-256,
  reruns selection, and asserts that the fixed four IDs did not change.

The solve process must not read the answer key, raw upstream CSV, grading
outputs, or this preparation tool. Fetching official data is a preparation
step; the solver has no web search or network tool. Merely storing files
separately does not itself create an OS security boundary: the runner must
enforce which files and tools it exposes to its model.

From the project root, rebuild from an existing local source file, or explicitly
fetch the exact pinned source during preparation:

```powershell
.\.venv\Scripts\python.exe fixtures\imo_answerbench\rebuild.py --source C:\path\answerbench_v2.csv --check
.\.venv\Scripts\python.exe fixtures\imo_answerbench\rebuild.py --fetch --check
```

Omit `--check` to regenerate fixture files. The entire source CSV is processed
in memory and is not copied into the solver's fixture.

## Grading protocol and limits

The official paper's §2.3 and Appendix A.5 use Gemini 2.5 Pro as
AnswerAutoGrader. It extracts a final answer and compares its mathematical
meaning to the reference answer. Algebraic and numerical equivalents count;
sets are unordered unless the problem specifies an ordered object. Incomplete,
wrong, or absent final answers receive no credit. Reasoning quality is not
scored. This is binary answer correctness, unlike the 0–7 proof scores in
IMO-ProofBench. See [the official paper](https://aclanthology.org/2025.emnlp-main.1794.pdf).

Our independent offline grader is a **local conservative answer-equivalence
check**, not the official Gemini AnswerAutoGrader. It does not call a model or
the network. It only marks an answer correct when its supported normalization
or exact symbolic computation establishes equivalence; unsupported or ambiguous
answers remain `ungraded`. The local grader evaluates final answers only and
does not certify a proof. Four-case results must identify the grading method,
ungraded count, completion states, and any run failures, and must not be reported
as the paper's 400-problem benchmark score.

After a batch completes, grade it independently from the project root:

```powershell
.\.venv\Scripts\python.exe scripts\score_answerbench.py --batch-dir data\answerbench\YOUR_BATCH
```

The default output is `scores.json` inside that batch directory. This scorer
uses SymPy through an allowlisted AST constructor and a separate subprocess with
a three-second limit per answer; it never evaluates input as Python code. Missing
answers and missing case reports count as incorrect, incomplete runs with a
candidate answer remain ungraded, and all four cases stay in the denominator.

```bibtex
@inproceedings{luong-etal-2025-towards,
  title = {Towards Robust Mathematical Reasoning},
  author = {Luong, Thang and Hwang, Dawsen and Nguyen, Hoang H. and others},
  booktitle = {Proceedings of the 2025 Conference on Empirical Methods in Natural Language Processing},
  year = {2025},
  url = {https://aclanthology.org/2025.emnlp-main.1794/}
}
```
