"""Offline grading must establish equivalence and never turn ambiguity into success."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("score_answerbench", ROOT / "scripts" / "score_answerbench.py")
grader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grader)


@pytest.mark.parametrize("answer", ["58", "$58$", r"\boxed{58}", "116/2", "58.0", r"\frac{116}{2}", "a+b=58"])
def test_numeric_equivalence(answer):
    assert grader.compare_worker(answer, "58", "integer")["grade"] == "correct"


@pytest.mark.parametrize("answer", ["57", "59", "580", "58.01", "116/3"])
def test_wrong_numeric_values_are_incorrect(answer):
    assert grader.compare_worker(answer, "58", "integer")["grade"] == "incorrect"


@pytest.mark.parametrize("answer", [r"2^{u-2}", "2**(u-2)", r"\frac{2^u}{4}", "2**u/4", "4**(u/2-1)", "C=2^(u-2)"])
def test_parameter_expression_equivalence(answer):
    assert grader.compare_worker(answer, r"$2^{u-2}$", "symbolic_exponential")["grade"] == "correct"


def test_one_matching_parameter_value_does_not_establish_equivalence():
    result = grader.compare_worker("1", r"$2^{u-2}$", "symbolic_exponential")
    assert result == {"grade": "incorrect", "reason": "exact_parameter_counterexample"}


@pytest.mark.parametrize("answer", [
    "no solutions", "No solutions.", r"\varnothing", r"\emptyset", "∅", "{}",
    r"\boxed{\text{There are no such pairs}}", r"\text{No solutions.}", "无解",
])
def test_explicit_empty_set_equivalence(answer):
    assert grader.compare_worker(answer, "no solutions", "empty_solution_set")["grade"] == "correct"


@pytest.mark.parametrize("answer", [
    "no solutions or (2014,4028)", "There may be no solutions", "I cannot find a solution",
    "I don't know", "0", "not no solutions", "No solutions, except one pair",
])
def test_ambiguous_set_answers_are_ungraded(answer):
    assert grader.compare_worker(answer, "no solutions", "empty_solution_set")["grade"] == "ungraded"


@pytest.mark.parametrize("answer", [
    "58 or 59", "The answer could be 58", "58=59", "[58]", "True", "lambda: 58",
    "__import__('os').system('echo unsafe')", "(1).__class__", "sqrt.__globals__",
    "2**(2**64)", "9**999999", "1e99", "9" * 513,
    "2**u*(u-2)/(4*(u-2))",
])
def test_unsafe_or_unsupported_expressions_are_ungraded(answer):
    assert grader.compare_worker(answer, "58", "integer")["grade"] == "ungraded"


def test_symbolic_cancellation_does_not_hide_undefined_domain_point():
    result = grader.compare_worker("2**u*(u-2)/(4*(u-2))", "2**(u-2)", "symbolic_exponential")
    assert result["grade"] == "ungraded"


def test_malicious_input_cannot_create_a_file(tmp_path):
    sentinel = tmp_path / "executed.txt"
    answer = f"__import__('pathlib').Path({str(sentinel)!r}).write_text('unsafe')"
    assert grader.compare_worker(answer, "58", "integer")["grade"] == "ungraded"
    assert not sentinel.exists()


def test_final_answer_wins_over_correct_value_in_reasoning():
    report = {"final_answer": "59", "final_body": "An earlier guess was 58. Final answer: 59"}
    assert grader.final_from_report(report) == ("59", "final_answer")


def test_only_terminal_box_is_extracted():
    report = {"final_body": r"An earlier calculation gave \boxed{58}. But this is wrong."}
    assert grader.final_from_report(report)[0] is None
    report = {"final_body": r"The result is therefore $\boxed{\frac{116}{2}}$."}
    assert grader.final_from_report(report)[0] == r"\frac{116}{2}"


def test_incomplete_run_cannot_be_marked_correct():
    report = {"problem_id": "sample", "final_answer": "58", "completed": False}
    key = {"id": "sample", "short_answer": "58", "answer_type": "integer"}
    assert grader.score_report(report, key)["grade"] == "ungraded"


def test_missing_final_answer_is_incorrect():
    report = {"problem_id": "sample", "final_body": "I am still working.", "completed": True}
    key = {"id": "sample", "short_answer": "58", "answer_type": "integer"}
    result = grader.score_report(report, key)
    assert result["reason"] == "missing_final_answer"
    assert result["answer_status"] == "missing_final_answer"
    assert result["grade"] == "incorrect"


def test_scope_draft_cannot_supply_a_missing_final_answer():
    report = {"problem_id": "sample", "final_answer": None,
              "final_body": "This is a draft awaiting review.",
              "scope": r"Tentatively \boxed{58}.", "completed": False,
              "state": "completed", "independent_reviews": 1}
    key = {"id": "sample", "short_answer": "58", "answer_type": "integer"}
    result = grader.score_report(report, key)
    assert result["answer_status"] == "missing_final_answer"
    assert result["runtime_completed"] is True
    assert result["runtime_terminal"] is True
    assert result["final_answer_present"] is False
    assert result["completed"] is False
    assert result["review_completed"] is None
    assert result["workflow_completed"] is None


def test_mathematical_error_has_a_distinct_answer_status(monkeypatch):
    monkeypatch.setattr(grader, "compare", grader.compare_worker)
    report = {"problem_id": "sample", "final_answer": "59", "completed": True,
              "state": "completed", "review_completed": True, "workflow_completed": True}
    key = {"id": "sample", "short_answer": "58", "answer_type": "integer"}
    result = grader.score_report(report, key)
    assert result["grade"] == "incorrect"
    assert result["answer_status"] == "mathematically_incorrect"
    assert result["reason"] == "exact_numeric_inequality"
    assert result["review_completed"] is True
    assert result["workflow_completed"] is True


def test_timeout_is_ungraded(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(grader.subprocess, "run", timeout)
    assert grader.compare("58", "58", "integer")["reason"] == "grader_timeout"


def test_subprocess_can_grade_without_a_model_or_network():
    assert grader.compare("116/2", "58", "integer") == {
        "grade": "correct", "reason": "exact_symbolic_equivalence",
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv redirector regression")
def test_windows_worker_uses_direct_interpreter_and_venv_libraries(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "unrelated-library-directory")
    monkeypatch.setenv("PYTHONHOME", "unrelated-python-home")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "unrelated-launcher")
    executable, settings = grader.worker_process_settings()
    assert executable == sys._base_executable
    assert settings["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert settings["env"]["PYTHONPATH"] == sysconfig.get_path("purelib")
    assert settings["env"]["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONHOME" not in settings["env"]
    assert "__PYVENV_LAUNCHER__" not in settings["env"]
    assert os.environ["PYTHONHOME"] == "unrelated-python-home"
    process = subprocess.run(
        [executable, "-c", "import json, sympy; print(json.dumps(sympy.__file__))"],
        capture_output=True, text=True, check=True, timeout=3, **settings,
    )
    assert Path(json.loads(process.stdout)).is_relative_to(Path(sysconfig.get_path("purelib")))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv redirector regression")
def test_windows_real_timeout_kills_the_actual_worker_pid(tmp_path, monkeypatch):
    slow_worker = tmp_path / "slow_grader_worker.py"
    slow_worker.write_text(
        "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(5)\n", encoding="utf-8",
    )
    # Redirect only this test's worker script, never a real evaluation batch.
    monkeypatch.setattr(grader, "__file__", str(slow_worker))
    original_popen = subprocess.Popen
    original_run = subprocess.run
    workers = []
    timeout_output = []

    def record_worker(*args, **kwargs):
        worker = original_popen(*args, **kwargs)
        workers.append(worker)
        return worker

    def record_timeout(*args, **kwargs):
        try:
            return original_run(*args, **kwargs)
        except subprocess.TimeoutExpired as exc:
            timeout_output.append(exc.output)
            raise

    monkeypatch.setattr(grader.subprocess, "Popen", record_worker)
    monkeypatch.setattr(grader.subprocess, "run", record_timeout)
    started = time.monotonic()
    try:
        result = grader.compare("58", "58", "integer", timeout=0.8)
        elapsed = time.monotonic() - started
        assert result == {"grade": "ungraded", "reason": "grader_timeout"}
        assert elapsed < 3
        assert len(workers) == 1
        # PID from inside Python must be the process that subprocess.run killed.
        assert int(timeout_output[0]) == workers[0].pid
        assert workers[0].returncode is not None
        assert workers[0].poll() is not None
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=3)


def test_full_batch_keeps_failures_and_ungraded_in_denominator(tmp_path, monkeypatch):
    key_path = tmp_path / "key.json"
    keys = [{"id": f"problem-{i}", "short_answer": "58", "answer_type": "integer"} for i in range(4)]
    key_path.write_text(json.dumps({"answers": keys}), encoding="utf-8")
    for index, answer in enumerate(["58", "59", "58 or 59"]):
        directory = tmp_path / f"problem-{index}"
        directory.mkdir()
        report = {"problem_id": f"problem-{index}", "final_answer": answer,
                  "completed": True, "state": "done", "interventions": []}
        (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(grader, "compare", grader.compare_worker)
    result = grader.score_batch(tmp_path, key_path)
    assert result["official_answer_autograder"] is False
    assert result["scorer_revision"] == grader.SCORER_REVISION
    assert result["scorer_sha256"] == hashlib.sha256(Path(grader.__file__).read_bytes()).hexdigest()
    assert result["summary"] == {
        "total": 4, "correct": 1, "incorrect": 2, "ungraded": 1, "completed": 3,
        "mathematically_incorrect": 1, "missing_final_answer": 0,
        "missing_case_report": 1, "invalid_case_report": 0,
        "completed_without_interventions": 3, "verified_correct_fraction_all_cases": 0.25,
        "runtime_completed": 0, "final_answers_present": 3,
        "review_completed_reported": 0, "review_completion_unknown": 4,
    }


def test_rescoring_uses_a_new_artifact_and_preserves_original(tmp_path):
    original = tmp_path / "scores.json"
    original.write_bytes(b'{"original": true}\n')
    destination = grader.default_score_output(tmp_path)
    assert destination == tmp_path / f"scores.v{grader.SCORER_REVISION}.json"
    grader.write_score_output(destination, {"scorer_revision": grader.SCORER_REVISION})
    assert original.read_bytes() == b'{"original": true}\n'
    with pytest.raises(FileExistsError):
        grader.write_score_output(original, {"overwrite": True})
    assert original.read_bytes() == b'{"original": true}\n'
    assert grader.default_score_output(tmp_path) not in {original, destination}


def test_fixture_selection_and_problem_answer_separation():
    directory = ROOT / "fixtures" / "imo_answerbench"
    problems = json.loads((directory / "problem-only.json").read_text(encoding="utf-8"))
    answers = json.loads((directory / "answer-key.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    rule = json.loads((directory / "selection-rule.json").read_text(encoding="utf-8"))
    expected_ids = []
    for family in ["algebra", "combinatorics", "geometry", "number_theory"]:
        ids = [f"imo-bench-{family}-{index:03d}" for index in range(1, 101)]
        expected_ids.append(min(ids, key=lambda value: hashlib.sha256(
            f"{rule['seed']}|{value}".encode("utf-8")).hexdigest()))
    assert [item["id"] for item in problems["problems"]] == expected_ids
    assert [item["id"] for item in answers["answers"]] == expected_ids
    assert len({item["category"] for item in problems["problems"]}) == 4
    assert all(set(item) == {"id", "category", "problem"} for item in problems["problems"])
    for name, digest in manifest["artifact_sha256"].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
