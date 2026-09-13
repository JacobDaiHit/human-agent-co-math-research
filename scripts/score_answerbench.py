"""Offline conservative answer equivalence; NOT the official Gemini AnswerAutoGrader.

Run after solving. Only this independent process opens the answer key. No model,
network call, eval, sympify, or parse_expr is used. Unsupported input is ungraded.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
from collections import Counter
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY = ROOT / "fixtures" / "imo_answerbench" / "answer-key.json"
METHOD = "local_conservative_answer_equivalence"
SCORER_REVISION = "3"
MAX_ANSWER_LENGTH = 512
MAX_AST_NODES = 96
MAX_AST_DEPTH = 14
MAX_INTEGER_DIGITS = 32
MAX_POWER = 64
CASE_TIMEOUT_SECONDS = 3.0
RUNTIME_TERMINAL_STATES = {
    "completed", "failed", "cancelled", "paused", "interrupted", "budget_exhausted",
    "step_limit", "reconciliation_required", "runner_error",
}


class UnsupportedAnswer(ValueError):
    """Input cannot be safely or unambiguously handled by the local grader."""


def braced(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != "{":
        raise UnsupportedAnswer("Expected a braced LaTeX argument")
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    raise UnsupportedAnswer("Unclosed LaTeX braces")


def unwrap(text: str) -> str:
    text = text.strip().removesuffix(".").strip()
    for _ in range(4):
        previous = text
        for opening, closing in [("$$", "$$"), ("$", "$"), (r"\[", r"\]"), (r"\(", r"\)")]:
            if text.startswith(opening) and text.endswith(closing):
                text = text[len(opening):-len(closing)].strip()
                break
        for command in [r"\boxed", r"\text", r"\mathrm", r"\operatorname"]:
            if text.startswith(command + "{"):
                value, end = braced(text, len(command))
                if end == len(text):
                    text = value.strip()
                    break
        if text == previous:
            break
    return text.removesuffix(".").strip()


def final_from_report(report: dict) -> tuple[str | None, str]:
    if report.get("report_schema_version") == "2.0":
        selected = report.get("answer_submission") or {}
        if selected.get("status") != "submitted":
            return None, "no_declared_submission"
        body = report.get("final_body") or ""
        answer = selected.get("answer")
        if (selected.get("selection_rule") != "last-root-step-explicit-finish-single-box-v1"
                or selected.get("body_sha256") != hashlib.sha256(body.encode()).hexdigest()
                or not isinstance(answer, str) or not answer.strip()):
            raise UnsupportedAnswer("Invalid frozen submission receipt")
        return answer, "declared_submission_v1"
    answer = report.get("final_answer")
    if isinstance(answer, str) and answer.strip():
        return answer, "final_answer"
    body = report.get("final_body")
    if not isinstance(body, str) or not body.strip():
        return None, "absent"
    if len(body) > 250_000:
        raise UnsupportedAnswer("Final body exceeds extraction limit")
    # Only accept a terminal boxed result; never search reasoning for a gold value.
    marker = body.rfind(r"\boxed{")
    if marker >= 0:
        value, end = braced(body, marker + len(r"\boxed"))
        if not body[end:].strip(" \r\n\t.$*\\])"):
            return value, "terminal_boxed_final_body"
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    match = re.fullmatch(r"(?:Final answer|Answer|最终答案|答案)\s*[:：]\s*(.+)", lines[-1], re.I)
    if match:
        return match.group(1), "terminal_final_answer_line"
    return None, "no_unambiguous_terminal_answer"


def latex_to_arithmetic(text: str) -> str:
    text = unwrap(text)
    for prefix in ("Final answer:", "Answer:", "最终答案：", "答案："):
        if text.lower().startswith(prefix.lower()):
            text = unwrap(text[len(prefix):])
            break
    if "=" in text:
        if text.count("=") != 1:
            raise UnsupportedAnswer("Multiple equalities in final answer")
        left, text = text.split("=", 1)
        if re.sub(r"\s+", "", left) not in {"C", "C_min", r"C_{\min}", "a+b"}:
            raise UnsupportedAnswer("Unsupported answer assignment")
        text = unwrap(text)
    for old, new in [("−", "-"), ("×", "*"), ("÷", "/"), (r"\cdot", "*"), (r"\times", "*")]:
        text = text.replace(old, new)
    for command in [r"\left", r"\right", r"\,", r"\;", r"\!", r"\ "]:
        text = text.replace(command, "")

    def convert(value: str, depth: int = 0) -> str:
        if depth > 10:
            raise UnsupportedAnswer("Too many nested LaTeX fractions")
        result = []
        index = 0
        while index < len(value):
            command = next((item for item in (r"\dfrac", r"\tfrac", r"\frac")
                            if value.startswith(item, index)), None)
            if command:
                index += len(command)
                numerator, index = braced(value, index)
                denominator, index = braced(value, index)
                result.append(f"(({convert(numerator, depth + 1)})/({convert(denominator, depth + 1)}))")
            elif value.startswith(r"\sqrt", index):
                value_inside, index = braced(value, index + len(r"\sqrt"))
                result.append(f"sqrt({convert(value_inside, depth + 1)})")
            else:
                result.append(value[index])
                index += 1
        return "".join(result)

    text = convert(text).replace("^", "**").replace("{", "(").replace("}", ")").strip()
    if len(text) > 2_048:
        raise UnsupportedAnswer("Normalized expression exceeds size limit")
    return text


def safe_expression(text: str, allowed_symbols: set[str]):
    """Construct SymPy objects only from allowlisted AST nodes, never input code."""
    import sympy as sp

    expression = latex_to_arithmetic(text)
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise UnsupportedAnswer("Unsupported arithmetic syntax") from exc
    if len(list(ast.walk(tree))) > MAX_AST_NODES:
        raise UnsupportedAnswer("Expression has too many AST nodes")

    def construct(node: ast.AST, depth: int = 0, power_depth: int = 0):
        if depth > MAX_AST_DEPTH:
            raise UnsupportedAnswer("Expression is too deeply nested")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            token = ast.get_source_segment(expression, node)
            if not token or len(token) > MAX_INTEGER_DIGITS:
                raise UnsupportedAnswer("Numeric literal exceeds limit")
            if not re.fullmatch(r"\d+(?:\.\d*)?(?:[eE][+-]?\d{1,2})?", token):
                raise UnsupportedAnswer("Unsupported numeric literal")
            number = Fraction(token)
            if max(abs(number.numerator).bit_length(), number.denominator.bit_length()) > 256:
                raise UnsupportedAnswer("Numeric literal exceeds bit limit")
            return sp.Rational(number.numerator, number.denominator)
        if isinstance(node, ast.Name) and node.id in allowed_symbols:
            return sp.Symbol(node.id, integer=True, positive=True)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = construct(node.operand, depth + 1, power_depth)
            return operand if isinstance(node.op, ast.UAdd) else sp.Mul(-1, operand, evaluate=False)
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id == "sqrt"
                    and len(node.args) == 1 and not node.keywords):
                raise UnsupportedAnswer("Only numeric sqrt is supported as a function")
            argument = construct(node.args[0], depth + 1, power_depth + 1)
            if not argument.is_number or argument.is_nonnegative is not True:
                raise UnsupportedAnswer("sqrt requires a nonnegative numeric argument")
            return sp.Pow(argument, sp.Rational(1, 2), evaluate=False)
        if not isinstance(node, ast.BinOp):
            raise UnsupportedAnswer("Unsupported expression node")
        is_power = isinstance(node.op, ast.Pow)
        if is_power and power_depth >= 2:
            raise UnsupportedAnswer("Too many nested powers")
        nested_power = power_depth + int(is_power)
        left = construct(node.left, depth + 1, nested_power)
        right = construct(node.right, depth + 1, nested_power)
        if isinstance(node.op, ast.Add):
            return sp.Add(left, right, evaluate=False)
        if isinstance(node.op, ast.Sub):
            return sp.Add(left, sp.Mul(-1, right, evaluate=False), evaluate=False)
        if isinstance(node.op, ast.Mult):
            return sp.Mul(left, right, evaluate=False)
        if isinstance(node.op, ast.Div):
            # Never cancel an unverified symbolic denominator and lose exclusions.
            if right.is_nonzero is not True:
                raise UnsupportedAnswer("Denominator is not provably nonzero")
            return sp.Mul(left, sp.Pow(right, -1, evaluate=False), evaluate=False)
        if is_power:
            if right.is_number:
                exponent = sp.simplify(right)
                if not exponent.is_Rational or abs(exponent) > MAX_POWER:
                    raise UnsupportedAnswer("Numeric exponent exceeds supported limit")
                if exponent.is_negative and left.is_nonzero is not True:
                    raise UnsupportedAnswer("Negative power of possibly zero base")
                return sp.Pow(left, exponent, evaluate=False)
            # Symbolic powers are restricted to a positive numeric base and an affine exponent.
            if not left.is_number or left.is_positive is not True:
                raise UnsupportedAnswer("Symbolic exponent requires a positive numeric base")
            symbols = sorted(right.free_symbols, key=str)
            if len(symbols) != 1:
                raise UnsupportedAnswer("Symbolic exponent must have one parameter")
            polynomial = sp.Poly(right, symbols[0])
            if polynomial.degree() > 1 or any(not c.is_Rational or abs(c) > MAX_POWER
                                               for c in polynomial.all_coeffs()):
                raise UnsupportedAnswer("Only bounded affine exponents are supported")
            return sp.Pow(left, right, evaluate=False)
        raise UnsupportedAnswer("Unsupported arithmetic operation")

    return construct(tree.body)


EMPTY_SET_FORMS = {
    "no solutions", "no solution", "there are no solutions", "there is no solution",
    "no such pairs", "there are no such pairs", "no pairs", "no positive integer pairs",
    "no pairs of positive integers", "no such positive integer pairs",
    "no such pair exists", "no such pairs exist", "there are no positive integer solutions",
    "there are no such pairs of positive integers",
    "there are no pairs of positive integers satisfying the conditions",
    "there are no pairs of positive integers satisfying these conditions",
    "不存在满足条件的正整数对", "不存在这样的正整数对", "无解",
    r"\emptyset", r"\varnothing", "∅", "{}", r"\{\}",
}


def compare_worker(candidate: str, golden: str, answer_type: str) -> dict:
    """A bounded subprocess invokes this; never invoke on arbitrary long input."""
    if len(candidate) > MAX_ANSWER_LENGTH or len(golden) > MAX_ANSWER_LENGTH:
        return {"grade": "ungraded", "reason": "answer_length_limit"}
    if answer_type == "empty_solution_set":
        if re.sub(r"\s+", " ", unwrap(golden)).strip().lower() not in EMPTY_SET_FORMS:
            return {"grade": "ungraded", "reason": "unsupported_reference_answer"}
        normalized = re.sub(r"\s+", " ", unwrap(candidate)).strip().lower()
        if normalized in EMPTY_SET_FORMS:
            return {"grade": "correct", "reason": "explicit_empty_solution_set"}
        return {"grade": "ungraded", "reason": "unsupported_or_ambiguous_set_answer"}
    if answer_type not in {"integer", "symbolic_exponential"}:
        return {"grade": "ungraded", "reason": "unsupported_answer_type"}
    try:
        import sympy as sp

        symbols = {"u"} if answer_type == "symbolic_exponential" else set()
        actual = safe_expression(candidate, symbols)
        expected = safe_expression(golden, symbols)
        difference = sp.simplify(actual - expected)
        if difference == 0:
            return {"grade": "correct", "reason": "exact_symbolic_equivalence"}
        if not difference.free_symbols and difference.is_zero is False:
            return {"grade": "incorrect", "reason": "exact_numeric_inequality"}
        # A counterexample proves inequality; matching sample values never prove equality.
        for symbol in difference.free_symbols:
            for integer in (2, 3, 4, 5, 10):
                value = sp.simplify(difference.subs(symbol, integer))
                if value.is_finite is True and value.is_zero is False:
                    return {"grade": "incorrect", "reason": "exact_parameter_counterexample"}
        return {"grade": "ungraded", "reason": "equivalence_not_established"}
    except ImportError:
        return {"grade": "ungraded", "reason": "sympy_not_installed"}
    except Exception:
        # Unsupported syntax, unsafe node, or symbolic engine errors are never a pass.
        return {"grade": "ungraded", "reason": "unsupported_or_unsafe_expression"}


def worker_process_settings() -> tuple[str, dict]:
    if sys.platform != "win32":
        return sys.executable, {}
    # A Windows venv python.exe redirects to another process. Launch the actual
    # interpreter so subprocess.run's timeout kills the computation's own PID.
    executable = getattr(sys, "_base_executable", None)
    purelib = sysconfig.get_path("purelib")
    if not executable or not purelib:
        raise UnsupportedAnswer("Cannot locate the direct Windows grading interpreter")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = purelib
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONHOME", None)
    environment.pop("__PYVENV_LAUNCHER__", None)
    return executable, {"env": environment, "creationflags": subprocess.CREATE_NO_WINDOW}


def compare(candidate: str, golden: str, answer_type: str, timeout: float = CASE_TIMEOUT_SECONDS) -> dict:
    payload = {"candidate": candidate, "golden": golden, "answer_type": answer_type}
    try:
        executable, process_settings = worker_process_settings()
        process = subprocess.run(
            [executable, str(Path(__file__).resolve()), "--worker"],
            input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True,
            timeout=timeout, check=False, **process_settings,
        )
        if process.returncode != 0:
            return {"grade": "ungraded", "reason": "grader_worker_failed"}
        result = json.loads(process.stdout)
        if result.get("grade") not in {"correct", "incorrect", "ungraded"}:
            return {"grade": "ungraded", "reason": "invalid_grader_worker_output"}
        return result
    except subprocess.TimeoutExpired:
        return {"grade": "ungraded", "reason": "grader_timeout"}
    except (OSError, ValueError):
        return {"grade": "ungraded", "reason": "grader_worker_failed"}


def classify_result(result: dict) -> dict:
    """Separate the benchmark's no-credit label from mathematical disproof."""
    reason = result["reason"]
    if reason in {"missing_final_answer", "missing_case_report", "invalid_case_report"}:
        answer_status = reason
    elif result["grade"] == "correct":
        answer_status = "correct"
    elif reason in {"exact_numeric_inequality", "exact_parameter_counterexample"}:
        answer_status = "mathematically_incorrect"
    else:
        answer_status = "ungraded"
    return {**result, "answer_status": answer_status}


def score_report(report: dict, key: dict) -> dict:
    common = {
        "problem_id": key["id"],
        "case_id": report.get("case_id"),
        "state": report.get("state"),
        "terminal_reason": report.get("terminal_reason"),
        "completed": report.get("completed") is True,
        "evaluation_mode": report.get("evaluation_mode", "legacy_research"),
        "solver": report.get("solver", "agent"),
        "proof_assessment": report.get("proof_assessment", {"status": "unrecorded"}),
        "usage_summary": report.get("usage_summary"),
        "requests": report.get("requests"),
        "financial_reconciliation_pending": report.get("financial_reconciliation_pending"),
        "interventions": report.get("interventions", []),
        "runtime_terminal": report.get("state") in RUNTIME_TERMINAL_STATES,
        "runtime_completed": report.get("state") == "completed",
        "final_answer_present": None,
        # An old report's review count cannot establish a completed review workflow.
        "review_completed": report.get("review_completed")
        if type(report.get("review_completed")) is bool else None,
        "workflow_completed": report.get("workflow_completed")
        if type(report.get("workflow_completed")) is bool else None,
    }
    if report.get("problem_id") != key["id"]:
        return classify_result({**common, "grade": "ungraded", "reason": "report_problem_id_mismatch"})
    try:
        candidate, extraction = final_from_report(report)
    except UnsupportedAnswer:
        return classify_result({**common, "grade": "ungraded", "reason": "unsupported_answer_extraction"})
    common.update({"extraction": extraction, "extracted_answer": candidate,
                   "final_answer_present": candidate is not None})
    if candidate is None:
        return classify_result({**common, "grade": "incorrect", "reason": "missing_final_answer"})
    if not common["completed"] and report.get("report_schema_version") != "2.0":
        return classify_result({**common, "grade": "ungraded", "reason": "run_not_completed"})
    return classify_result({**common, **compare(candidate, key["short_answer"], key["answer_type"])})


def score_batch(batch_dir: Path, answer_key: Path = DEFAULT_KEY, *, case_ids=None) -> dict:
    plan_path = batch_dir / "plan.json"
    if case_ids is None and plan_path.exists():
        case_ids = json.loads(plan_path.read_text(encoding="utf-8"))["configuration"]["case_ids"]
    raw_key = answer_key.read_bytes()
    keys = json.loads(raw_key)["answers"]
    if case_ids is not None:
        if not case_ids or len(set(case_ids)) != len(case_ids) or not set(case_ids) <= {key["id"] for key in keys}:
            raise ValueError("Select nonempty, unique, known case IDs")
        keys = [key for key in keys if key["id"] in case_ids]
    results = []
    for key in keys:
        report_path = batch_dir / key["id"] / "report.json"
        try:
            if report_path.stat().st_size > 64_000_000:
                raise ValueError("Report exceeds size limit")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            result = score_report(report, key)
        except FileNotFoundError:
            result = {"problem_id": key["id"], "grade": "incorrect",
                      "reason": "missing_case_report", "completed": False}
        except (ValueError, TypeError, KeyError, AttributeError):
            result = {"problem_id": key["id"], "grade": "ungraded",
                      "reason": "invalid_case_report", "completed": False}
        results.append({**classify_result(result), "report_path": str(report_path.resolve())})
    counts = Counter(result["grade"] for result in results)
    statuses = Counter(result["answer_status"] for result in results)
    return {
        "schema_version": "2.0",
        "benchmark": "IMO-AnswerBench selected cases",
        "scope": "targeted_retest" if case_ids is not None else "full_fixture",
        "case_ids": [key["id"] for key in keys],
        "grading_method": METHOD,
        "scorer_revision": SCORER_REVISION,
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scorer_revision_note": "Revision 3 grades predeclared schema-v2 answer submissions independently "
        "of internal proof review and workflow status. Legacy report completion gates remain unchanged; "
        "no earlier candidate is promoted into a final answer. Equivalence rules are unchanged.",
        "official_answer_autograder": False,
        "method_notice": "Local conservative offline check, not official Gemini AnswerAutoGrader. "
        "Only final-answer equivalence is assessed; proofs are not graded. "
        "Unsupported answers are ungraded. Binary incorrect includes missing answers or reports; "
        "it does not imply mathematical disproof. Runtime completion, final-answer presence, "
        "review completion and mathematical correctness are separate. "
        "Four samples are not full-benchmark accuracy.",
        "scored_at": datetime.now(UTC).isoformat(),
        "batch_dir": str(batch_dir.resolve()),
        "answer_key_sha256": hashlib.sha256(raw_key).hexdigest(),
        "per_case_timeout_seconds": CASE_TIMEOUT_SECONDS,
        "summary": {
            "total": len(results), "correct": counts["correct"], "incorrect": counts["incorrect"],
            "ungraded": counts["ungraded"],
            "mathematically_incorrect": statuses["mathematically_incorrect"],
            "missing_final_answer": statuses["missing_final_answer"],
            "missing_case_report": statuses["missing_case_report"],
            "invalid_case_report": statuses["invalid_case_report"],
            "completed": sum(result["completed"] for result in results),
            "completed_without_interventions": sum(result["completed"] and
                                                     not result.get("interventions") for result in results),
            "runtime_completed": sum(result.get("runtime_completed") is True for result in results),
            "final_answers_present": sum(result.get("final_answer_present") is True for result in results),
            "review_completed_reported": sum(result.get("review_completed") is True for result in results),
            "review_completion_unknown": sum(result.get("review_completed") is None for result in results),
            "verified_correct_fraction_all_cases": counts["correct"] / len(results) if results else None,
        },
        "cases": results,
    }


def default_score_output(batch_dir: Path) -> Path:
    original = batch_dir / "scores.json"
    if not original.exists():
        return original
    revised = batch_dir / f"scores.v{SCORER_REVISION}.json"
    if not revised.exists():
        return revised
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return batch_dir / f"scores.v{SCORER_REVISION}.{suffix}.json"


def write_score_output(output: Path, result: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Preserve historical scoring artifacts, including an explicitly named output.
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", "--run-dir", type=Path)
    parser.add_argument("--answer-key", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case-id", action="append", help="Score only this explicitly selected case")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        payload = json.loads(sys.stdin.read(4_096))
        print(json.dumps(compare_worker(**payload)))
        return
    if args.batch_dir is None:
        parser.error("--batch-dir is required")
    result = score_batch(args.batch_dir, args.answer_key, case_ids=args.case_id)
    output = args.output or default_score_output(args.batch_dir)
    try:
        write_score_output(output, result)
    except FileExistsError:
        parser.error("Output already exists; choose a new --output path to preserve prior scores")
    print(json.dumps({"grading_method": METHOD, "summary": result["summary"], "output": str(output)}))


if __name__ == "__main__":
    main()
