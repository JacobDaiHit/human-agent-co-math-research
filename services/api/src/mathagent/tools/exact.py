"""Closed exact calculators with a bounded AST interpreter and immutable records.

There is no Python execution, caller-selected file access, process launch, or network call.
Input expressions are arithmetic data; only the explicitly listed AST nodes are
interpreted. Every intermediate coefficient and polynomial remains bounded.
"""

import ast
import io
import json
import keyword
import re
import time
import tokenize
from fractions import Fraction

from mathagent.application.errors import DomainError
from mathagent.application.research_records import _visible_reference
from mathagent.persistence.artifacts import ArtifactError, ArtifactStore
from mathagent.persistence.models import Dependency, ProofPlan, Review, now
from mathagent.tools.hermitian import exact_calculation
from sqlalchemy import select

TOOL_VERSION = "bounded-exact-v1"
LIMITS = {
    "expression_characters": 4096,
    "integer_digits": 100,
    "integer_bits": 1024,
    "tokens": 256,
    "ast_nodes": 128,
    "ast_depth": 24,
    "variables": 4,
    "polynomial_terms": 128,
    "total_degree": 16,
    "exponent": 16,
    "operation_count": 20_000,
    "output_characters": 32_000,
}
_VARIABLE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,15}\Z")
_INTEGER = re.compile(r"[0-9]+\Z")
_OPERATORS = {"+", "-", "*", "/", "**", "(", ")"}
_TOOLS = {"rational_arithmetic", "polynomial_identity", "hermitian_crossing"}


class CalculationError(ValueError):
    """A fixed diagnostic code; untrusted source text is not interpolated."""


def _exponent(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
            return node.operand.value * (-1 if isinstance(node.op, ast.USub) else 1)
    raise CalculationError("literal_integer_exponent_required")


def _parse(expression, variables, allow_negative_powers):
    # Bound token/constant/parenthesis complexity before calling Python's parser.
    depth = count = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(expression).readline):
            count += 1
            if count > LIMITS["tokens"]:
                raise CalculationError("token_limit_exceeded")
            if token.type == tokenize.NUMBER:
                if not _INTEGER.fullmatch(token.string):
                    raise CalculationError("integer_literals_only")
                if len(token.string) > LIMITS["integer_digits"]:
                    raise CalculationError("integer_literal_limit_exceeded")
            elif token.type == tokenize.NAME:
                if token.string not in variables:
                    raise CalculationError("undeclared_variable")
            elif token.type == tokenize.OP:
                if token.string not in _OPERATORS:
                    raise CalculationError("unsupported_operator")
                depth += (token.string == "(") - (token.string == ")")
                if depth > LIMITS["ast_depth"]:
                    raise CalculationError("expression_depth_exceeded")
            elif token.type not in {tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER}:
                raise CalculationError("unsupported_syntax")
        tree = ast.parse(expression, mode="eval")
    except (
        SyntaxError,
        tokenize.TokenError,
        IndentationError,
        RecursionError,
        ValueError,
    ) as error:
        if isinstance(error, CalculationError):
            raise
        raise CalculationError("invalid_expression") from None
    stack, count = [(tree, 0)], 0
    allowed = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.UAdd,
        ast.USub,
    )
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > LIMITS["ast_nodes"]:
            raise CalculationError("ast_node_limit_exceeded")
        if depth > LIMITS["ast_depth"]:
            raise CalculationError("expression_depth_exceeded")
        if not isinstance(node, allowed):
            raise CalculationError("unsupported_syntax")
        if isinstance(node, ast.Constant) and type(node.value) is not int:
            raise CalculationError("integer_literals_only")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            power = _exponent(node.right)
            if abs(power) > LIMITS["exponent"]:
                raise CalculationError("exponent_limit_exceeded")
            if power < 0 and not allow_negative_powers:
                raise CalculationError("polynomial_negative_power")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree.body


def _fraction_latex(value):
    if value.denominator == 1:
        return str(value.numerator)
    sign = "-" if value < 0 else ""
    return sign + rf"\frac{{{abs(value.numerator)}}}{{{value.denominator}}}"


def _latex(node):
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Name):
        escaped = node.id.replace("_", r"\_")
        return node.id if len(node.id) == 1 else rf"\mathit{{{escaped}}}"
    if isinstance(node, ast.UnaryOp):
        return (
            "-" if isinstance(node.op, ast.USub) else "+"
        ) + rf"\left({_latex(node.operand)}\right)"
    left, right = _latex(node.left), _latex(node.right)
    if isinstance(node.op, ast.Div):
        return rf"\frac{{{left}}}{{{right}}}"
    if isinstance(node.op, ast.Pow):
        return rf"\left({left}\right)^{{{_exponent(node.right)}}}"
    operator = {ast.Add: "+", ast.Sub: "-", ast.Mult: r"\cdot "}[type(node.op)]
    return rf"\left({left}{operator}{right}\right)"


class PolynomialInterpreter:
    def __init__(self, variables):
        self.variables = variables
        self.zero_power = (0,) * len(variables)
        self.operations = 0

    def tick(self, count=1):
        if self.operations + count > LIMITS["operation_count"]:
            raise CalculationError("operation_limit_exceeded")
        self.operations += count

    def coefficient(self, left, right, operation):
        self.tick()
        an, ad = abs(left.numerator).bit_length(), left.denominator.bit_length()
        bn, bd = abs(right.numerator).bit_length(), right.denominator.bit_length()
        if operation in {"add", "subtract"}:
            sizes = (max(an + bd, bn + ad) + 1, ad + bd)
        elif operation == "multiply":
            sizes = (an + bn, ad + bd)
        else:
            if right == 0:
                raise CalculationError("division_by_zero")
            sizes = (an + bd, ad + bn)
        if max(sizes) > LIMITS["integer_bits"]:
            raise CalculationError("intermediate_integer_limit_exceeded")
        value = {
            "add": lambda: left + right,
            "subtract": lambda: left - right,
            "multiply": lambda: left * right,
            "divide": lambda: left / right,
        }[operation]()
        if (
            max(abs(value.numerator).bit_length(), value.denominator.bit_length())
            > LIMITS["integer_bits"]
        ):
            raise CalculationError("intermediate_integer_limit_exceeded")
        return value

    def add(self, left, right, subtract=False):
        result = dict(left)
        for powers, coefficient in right.items():
            value = self.coefficient(
                result.get(powers, Fraction(0)), coefficient, "subtract" if subtract else "add"
            )
            if value:
                result[powers] = value
            else:
                result.pop(powers, None)
            if len(result) > LIMITS["polynomial_terms"]:
                raise CalculationError("polynomial_term_limit_exceeded")
        return result

    def multiply(self, left, right):
        # All term-pair work and possible degree growth are checked in advance.
        if self.operations + 2 * len(left) * len(right) > LIMITS["operation_count"]:
            raise CalculationError("operation_limit_exceeded")
        if left and right and max(map(sum, left)) + max(map(sum, right)) > LIMITS["total_degree"]:
            raise CalculationError("polynomial_degree_limit_exceeded")
        result = {}
        for lp, lc in left.items():
            for rp, rc in right.items():
                powers = tuple(a + b for a, b in zip(lp, rp, strict=True))
                value = self.coefficient(lc, rc, "multiply")
                value = self.coefficient(result.get(powers, Fraction(0)), value, "add")
                if value:
                    result[powers] = value
                else:
                    result.pop(powers, None)
                if len(result) > LIMITS["polynomial_terms"]:
                    raise CalculationError("polynomial_term_limit_exceeded")
        return result

    def evaluate(self, node):
        self.tick()
        if isinstance(node, ast.Constant):
            return {self.zero_power: Fraction(node.value)} if node.value else {}
        if isinstance(node, ast.Name):
            return {tuple(int(name == node.id) for name in self.variables): Fraction(1)}
        if isinstance(node, ast.UnaryOp):
            value = self.evaluate(node.operand)
            return (
                {powers: -coefficient for powers, coefficient in value.items()}
                if isinstance(node.op, ast.USub)
                else value
            )
        left = self.evaluate(node.left)
        if isinstance(node.op, ast.Pow):
            exponent = _exponent(node.right)
            if exponent == 0 and not left:
                raise CalculationError("indeterminate_zero_power")
            if left and max(map(sum, left)) * max(0, exponent) > LIMITS["total_degree"]:
                raise CalculationError("polynomial_degree_limit_exceeded")
            if exponent < 0:
                if set(left) - {self.zero_power}:
                    raise CalculationError("polynomial_negative_power")
                denominator = left.get(self.zero_power, Fraction(0))
                left = {self.zero_power: self.coefficient(Fraction(1), denominator, "divide")}
                exponent = -exponent
            result = {self.zero_power: Fraction(1)}
            for _ in range(exponent):
                result = self.multiply(result, left)
            return result
        right = self.evaluate(node.right)
        if isinstance(node.op, ast.Add):
            return self.add(left, right)
        if isinstance(node.op, ast.Sub):
            return self.add(left, right, subtract=True)
        if isinstance(node.op, ast.Mult):
            return self.multiply(left, right)
        if set(right) - {self.zero_power}:
            raise CalculationError("nonconstant_polynomial_denominator")
        denominator = right.get(self.zero_power, Fraction(0))
        if denominator == 0:
            raise CalculationError("division_by_zero")
        return {
            powers: self.coefficient(coefficient, denominator, "divide")
            for powers, coefficient in left.items()
        }

    def terms(self, polynomial):
        return [
            {"powers": list(powers), "coefficient": str(polynomial[powers])}
            for powers in sorted(polynomial, key=lambda p: (sum(p), p), reverse=True)
        ]


def _inputs(tool, values):
    if not isinstance(values, dict):
        raise DomainError(422, "invalid_calculation_input", "计算输入必须是对象。")
    required = {
        "rational_arithmetic": {"expression"},
        "polynomial_identity": {"left", "right", "variables"},
        "hermitian_crossing": set(),
    }[tool]
    if set(values) != required:
        raise DomainError(422, "invalid_calculation_input", "计算输入字段不符合所选固定工具。")
    for field in required - {"variables"}:
        if (
            not isinstance(values[field], str)
            or not values[field].strip()
            or len(values[field]) > LIMITS["expression_characters"]
        ):
            raise DomainError(
                422, "invalid_calculation_input", "表达式不能为空且不能超过长度限制。"
            )
    if tool == "polynomial_identity":
        variables = values["variables"]
        if (
            not isinstance(variables, list)
            or not 1 <= len(variables) <= LIMITS["variables"]
            or any(
                not isinstance(v, str) or not _VARIABLE.fullmatch(v) or keyword.iskeyword(v)
                for v in variables
            )
            or len(set(variables)) != len(variables)
        ):
            raise DomainError(422, "invalid_calculation_input", "多项式需要互异的受限变量名。")
    return {
        key: list(value) if isinstance(value, list) else value.strip()
        for key, value in values.items()
    }


def execute_calculation(state, session, branch, values, *, author, run_id):
    """Persist one actual local calculation; caller owns transaction/idempotency."""
    if not isinstance(values, dict) or set(values) - {"tool", "inputs", "target_revision_id"}:
        raise DomainError(422, "invalid_calculation", "固定计算请求字段不符合协议。")
    tool = values.get("tool")
    if not isinstance(tool, str) or tool not in _TOOLS:
        raise DomainError(422, "unsupported_calculation", "没有这个固定精确计算工具。")
    inputs = _inputs(tool, values.get("inputs", {}))
    target_id = values.get("target_revision_id")
    if target_id is not None and (
        not isinstance(target_id, str) or not target_id or len(target_id) > 100
    ):
        raise DomainError(422, "invalid_calculation_target", "计算目标版本标识无效。")
    target, target_object = (
        _visible_reference(session, branch, target_id) if target_id else (None, None)
    )
    started, started_at = time.perf_counter(), now()
    interpreter = PolynomialInterpreter(inputs.get("variables", []))
    status, error, exact_result, stdout = "ok", None, None, ""
    formulas = []
    scope = {
        "rational_arithmetic": "仅检查输入有理数表达式的精确值；不验证目标论证的其他步骤。",
        "polynomial_identity": "仅比较输入的两个有理系数多项式；不覆盖目标的完整证明。",
        "hermitian_crossing": r"仅计算固定矩阵族 $H(t)=\begin{pmatrix}0&t\\t&0\end{pmatrix}$，参数 $t\in\mathbb{R}$；不推出一般谱理论或形式化证明。",
    }[tool]
    assumptions = (
        [r"参数 $t\in\mathbb{R}$，只使用给定固定矩阵族。"]
        if tool == "hermitian_crossing"
        else [r"系数与常数属于 $\mathbb{Q}$，分母必须非零。"]
    )
    if tool == "polynomial_identity":
        assumptions.append("变量是交换不定元；按多项式系数比较，不使用数值抽样。")
    try:
        if tool == "hermitian_crossing":
            exact_result = exact_calculation()
            formulas = [
                r"特征多项式为 $\lambda^2-t^2$，解析分支为 $t$ 与 $-t$。",
                r"排序后为 $-|t|$、$|t|$；在 $t=0$ 的左右导数分别为 $(1,-1)$ 和 $(-1,1)$。",
            ]
        elif tool == "rational_arithmetic":
            node = _parse(inputs["expression"], [], True)
            value = interpreter.evaluate(node).get((), Fraction(0))
            exact_result = {
                "value": str(value),
                "numerator": str(value.numerator),
                "denominator": str(value.denominator),
            }
            formulas = ["$" + _latex(node) + "=" + _fraction_latex(value) + "$。"]
        else:
            left_node = _parse(inputs["left"], inputs["variables"], False)
            right_node = _parse(inputs["right"], inputs["variables"], False)
            left, right = interpreter.evaluate(left_node), interpreter.evaluate(right_node)
            difference = interpreter.add(left, right, subtract=True)
            exact_result = {
                "identity": not difference,
                "variables": inputs["variables"],
                "left": interpreter.terms(left),
                "right": interpreter.terms(right),
                "difference": interpreter.terms(difference),
            }
            formulas = [
                "$"
                + _latex(left_node)
                + ("=" if not difference else r"\ne ")
                + _latex(right_node)
                + "$。"
            ]
        stdout = json.dumps(exact_result, ensure_ascii=False, sort_keys=True)
        if len(stdout) > LIMITS["output_characters"]:
            raise CalculationError("output_limit_exceeded")
    except CalculationError as failure:
        status, error, exact_result, stdout = "error", str(failure), None, ""
        formulas = ["计算未完成；错误代码：" + error + "。"]
    payload = {
        "artifact_type": "exact_calculation",
        "tool": tool,
        "tool_version": TOOL_VERSION,
        "input": inputs,
        "exact_result": exact_result,
        "scope": scope,
        "assumptions": assumptions,
        "stdout": stdout,
        "error": error,
        "status": status,
        "runtime": {
            "engine": "Python fractions.Fraction; fixed Hermitian family"
            if tool == "hermitian_crossing"
            else "Python ast + fractions.Fraction",
            "started_at": started_at,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "operation_count": interpreter.operations,
            "operation_count_kind": "interpreter_steps_and_coefficient_operations",
            "limits": dict(LIMITS),
            "network": False,
            "subprocess": False,
        },
        "execution": "actual_local_exact_calculation",
        "simulated": False,
        "candidate": True,
        "evidence_kind": "exact_computation",
        "run_id": run_id,
        "requested_by": author,
        "target_revision_id": target_id,
    }
    try:
        payload["artifact_files"] = [ArtifactStore(state.db.path).write_json(payload)]
    except ArtifactError as error:
        raise DomainError(
            503, "artifact_storage_failed", "计算产物文件保存失败；未提交计算记录。"
        ) from error
    title = {
        "rational_arithmetic": "有理数精确计算",
        "polynomial_identity": "多项式恒等式检查",
        "hermitian_crossing": "固定 Hermitian 交叉计算",
    }[tool]
    body = (
        f"# {title}\n\n"
        + "\n\n".join(formulas)
        + "\n\n范围："
        + scope
        + "\n\n"
        + "\n".join(assumptions)
    )
    tool_author = "exact_tool:" + tool
    obj, revision = state.new_object(session, branch, "artifact", body, payload, tool_author)
    state._add_reference(session, branch.id, revision.id)
    review_id = None
    if target:
        refs = {target_object.id: target.id, obj.id: revision.id}
        plan = session.get(ProofPlan, target.id)
        if plan:
            dependencies = session.scalars(
                select(Dependency).where(Dependency.plan_revision_id == plan.revision_id)
            ).all()
            for revision_id in [
                plan.conclusion_revision_id,
                *(d.revision_id for d in dependencies),
            ]:
                dependency = state.require_revision(session, revision_id)
                refs[dependency.object_id] = dependency.id
        verdict = (
            "inconclusive"
            if status == "error"
            else "issues"
            if tool == "polynomial_identity" and not exact_result["identity"]
            else "passed"
        )
        review = Review(
            project_id=branch.project_id,
            target_revision_id=target.id,
            kind="exact_computation",
            verdict=verdict,
            coverage="partial",
            scope=scope,
            findings=formulas,
            dependency_snapshot=refs,
            author=tool_author,
        )
        session.add(review)
        session.flush()
        review_id = review.id
        state.emit(
            session,
            branch.project_id,
            branch.id,
            "review.recorded",
            {
                "review_id": review.id,
                "target_revision_id": target.id,
                "artifact_revision_id": revision.id,
                "coverage": "partial",
                "provenance": "actual_local_exact_calculation",
                "run_id": run_id,
            },
            tool_author,
        )
    response = {"object_id": obj.id, "revision_id": revision.id, "review_id": review_id, **payload}
    state.emit(
        session,
        branch.project_id,
        branch.id,
        "calculation.completed",
        {
            "object_id": obj.id,
            "revision_id": revision.id,
            "review_id": review_id,
            "tool": tool,
            "tool_version": TOOL_VERSION,
            "status": status,
            "run_id": run_id,
        },
        tool_author,
    )
    return response
