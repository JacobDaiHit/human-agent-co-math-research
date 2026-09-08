"""A closed, exact elementary calculation and explicitly authored research example.

There is no code input, symbolic parser, subprocess, network, or model call here.
The only supported family is H(t)=[[0,t],[t,0]], for a real parameter t.
"""

from fractions import Fraction

from mathagent.persistence.models import ResearchObject, Review, Revision

EXAMPLE_SOURCE = "pre_authored_hermitian_example_v1"
ORIGINAL_QUESTION = (
    "对实参数 $t$，若 $H(t)$ 是矩阵元素关于 $t$ 解析的 Hermitian 矩阵族，"
    "把特征值从小到大排序后，每个特征值都在 $t=0$ 可微。这个猜想是否正确？"
)


def exact_calculation():
    """Evaluate determinant coefficients, eigenvectors, and one-sided slopes exactly."""
    zero, one = Fraction(0), Fraction(1)
    # H(t)=t*A. Compute the 2x2 determinant and eigenvector equations over Q.
    a = ((zero, one), (one, zero))
    determinant = a[0][0] * a[1][1] - a[0][1] * a[1][0]
    trace = a[0][0] + a[1][1]
    branches = []
    for coefficient, vector in ((one, (one, one)), (-one, (one, -one))):
        actual = tuple(sum(a[i][j] * vector[j] for j in range(2)) for i in range(2))
        expected = tuple(coefficient * entry for entry in vector)
        assert actual == expected
        assert coefficient**2 - trace * coefficient + determinant == zero
        branches.append(
            {
                "coefficient_of_t": str(coefficient),
                "eigenvector": [str(value) for value in vector],
                "Av": [str(value) for value in actual],
            }
        )
    slopes = {}
    samples = []
    for t in (Fraction(-2), Fraction(-1, 2), zero, Fraction(1, 2), Fraction(2)):
        eigenvalues = sorted((t, -t))
        samples.append({"t": str(t), "sorted_eigenvalues": list(map(str, eigenvalues))})
    # On each open half-line ordering of +/-t is fixed, so these coefficients
    # are the exact slopes on an entire interval, not numerical limit estimates.
    for side, sign in (("left", -one), ("right", one)):
        values = sorted((sign, -sign))
        slopes[side] = [str(value / sign) for value in values]
    return {
        "tool": "fixed_hermitian_2x2",
        "tool_version": 1,
        "execution": "actual_local_exact_calculation",
        "simulated": False,
        "engine": "Python fractions.Fraction; rational matrix arithmetic",
        "matrix": [["0", "t"], ["t", "0"]],
        "parameter_domain": "real",
        "characteristic_polynomial": {
            "lambda^2": "1",
            "lambda*t": str(-trace),
            "t^2": str(determinant),
        },
        "analytic_branches": branches,
        "sorted_eigenvalues": ["-|t|", "|t|"],
        "one_sided_derivatives_at_zero": slopes,
        "rational_samples": samples,
        "scope": r"Only $H(t)=\begin{pmatrix}0&t\\t&0\end{pmatrix}$; no general spectral theorem or formal proof checker.",
        "outcome": "Both sorted eigenvalue functions fail to be differentiable at zero.",
        "method": r"Eigenvector identities establish roots $\pm t$; fixed ordering on each "
        "half-line gives exact one-sided slopes. Samples are illustrations only.",
    }


class HermitianExample:
    def __init__(self, service):
        self.service = service

    def create(self, session, _payload):
        service = self.service
        _, project = service.create_project(
            session,
            {
                "title": "Hermitian 交叉与排序 · 可复算示例",
                "body": ORIGINAL_QUESTION,
            },
        )
        main = project["branch_id"]
        named = {
            "original_question": {
                "object_id": project["object_id"],
                "revision_id": project["revision_id"],
            }
        }

        def label(result):
            obj = session.get(ResearchObject, result["object_id"])
            rev = session.get(Revision, result["revision_id"])
            obj.author = rev.author = "example_pre_authored"
            rev.payload = {
                **rev.payload,
                "source": EXAMPLE_SOURCE,
                "pre_authored": True,
                "model_discovery": False,
            }
            return result

        label(named["original_question"])

        def obj(name, kind, body, payload=None, branch=main):
            _, result = service.create_object(
                session,
                {
                    "branch_id": branch,
                    "kind": kind,
                    "body": body,
                    "payload": payload or {},
                },
            )
            named[name] = label(result)
            return result

        def proof(name, conclusion, body, *, contexts=(), assumptions=(), gaps=(), branch=main):
            _, result = service.create_proof(
                session,
                {
                    "branch_id": branch,
                    "conclusion_revision_id": named[conclusion]["revision_id"],
                    "body": body,
                    "premise_revision_ids": [],
                    "context_revision_ids": [named[n]["revision_id"] for n in contexts],
                    "assumption_revision_ids": [named[n]["revision_id"] for n in assumptions],
                    "gaps": list(gaps),
                    "rule": "direct",
                },
            )
            named[name] = label(result)
            return result

        def relation(source, target, kind, branch=main):
            service.add_relation(
                session,
                {
                    "branch_id": branch,
                    "source_id": named[source]["object_id"],
                    "target_id": named[target]["object_id"],
                    "kind": kind,
                },
            )

        def adoption(name, state, reason, branch=main):
            service.adopt(
                session,
                {
                    "branch_id": branch,
                    "revision_id": named[name]["revision_id"],
                    "state": state,
                    "reason": "预写示例状态：" + reason,
                },
            )

        obj(
            "real_parameter",
            "context",
            r"$t\in\mathbb{R}$；研究 $t=0$ 两侧的同一开区间。",
            {"role": "assumption"},
        )
        obj(
            "sorting",
            "context",
            r"Hermitian 指 $H(t)^*=H(t)$。排序标签满足 $\lambda_1(t)\le\lambda_2(t)$；它与沿参数连续跟踪的分支标签不同。",
            {"role": "definition"},
        )
        obj("conjecture", "claim", "原猜想：实解析 Hermitian 族的每个排序特征值都在 $0$ 可微。")
        proof(
            "failed_argument",
            "conjecture",
            "尝试：由矩阵元素解析推断排序后的特征值也解析。"
            "交叉处可能交换排序标签，这一步没有成立。",
            contexts=("sorting",),
            assumptions=("real_parameter",),
            gaps=("缺少排序操作在重特征值处保持可微的理由。",),
        )
        relation("failed_argument", "original_question", "attempts")
        adoption("conjecture", "disputed", "保留被反驳的原猜想，不改写为新问题。")
        adoption("failed_argument", "withdrawn", "排序与解析标签混淆，论证路线失败。")
        obj(
            "failed_route",
            "activity",
            "数学路线失败记录：排序不保持交叉点处的可微性；这不是运行失败，也不是模型/API 故障。",
            {"outcome": "mathematical_route_failed", "failure_reason": "sorting_at_crossing"},
        )
        result = exact_calculation()
        obj(
            "exact_artifact",
            "artifact",
            r"实际本地精确计算：$H(t)=\begin{pmatrix}0&t\\t&0\end{pmatrix}$，"
            r"$\det(\lambda I-H)=\lambda^2-t^2$；解析分支为 $t$ 与 $-t$。"
            r"排序为 $-|t|$、$|t|$，左导数分别为 $1$、$-1$，右导数分别为 $-1$、$1$。",
            result,
        )
        obj(
            "counterexample",
            "claim",
            r"反例：$H(t)=\begin{pmatrix}0&t\\t&0\end{pmatrix}$ 的元素为实多项式且矩阵 Hermitian，"
            r"但排序后的 $\lambda_1=-|t|$、$\lambda_2=|t|$ 在 $t=0$ 都不可微。",
        )
        proof(
            "counterexample_argument",
            "counterexample",
            "$H(t)$ 实对称，故为 Hermitian；元素为多项式。"
            r"计算 $\det(\lambda I-H)=\lambda^2-t^2=(\lambda-t)(\lambda+t)$，并验算特征向量 $(1,1)$、$(1,-1)$。"
            "对 $t>0$ 排序为 $(-t,t)$，对 $t<0$ 排序为 $(t,-t)$。"
            r"因此 $\lambda_2$ 左导数为 $-1$、右导数为 $1$；$\lambda_1$ 左导数为 $1$、右导数为 $-1$。"
            "左右导数不等，得到满足原假设的具体反例。",
            contexts=("sorting",),
            assumptions=("real_parameter",),
        )
        _, review = service.add_review(
            session,
            {
                "branch_id": main,
                "target_revision_id": named["counterexample_argument"]["revision_id"],
                "kind": "exact_computation",
                "verdict": "passed",
                "coverage": "partial",
                "scope": r"内置固定 $2\times2$ 计算器实际执行：有理系数行列式、特征向量恒等式和分段斜率。"
                "正文推导为预写示例；不是独立人工审查、模型发现或一般形式化验证。",
                "findings": [
                    "特征多项式系数为 $(1,0,-1)$。",
                    "左右导数分别为 $(1,-1)$、$(-1,1)$。",
                    "只覆盖此矩阵族的精确计算；完整文字论证仍待人工审查。",
                ],
            },
        )
        evidence = session.get(Review, review["review_id"])
        evidence.author = "fixed_hermitian_exact_tool"
        # Bind the actual tool record into the evidence snapshot as well as the
        # proof's inputs, so exporting only the counterexample keeps its output.
        evidence.dependency_snapshot = {
            **evidence.dependency_snapshot,
            named["exact_artifact"]["object_id"]: named["exact_artifact"]["revision_id"],
        }
        adoption("counterexample", "pending_review", "精确计算已完成；完整文字推导待用户审查。")
        adoption("counterexample_argument", "pending_review", "预写论证不冒充用户已审查。")
        relation("counterexample", "conjecture", "refutes")
        relation("exact_artifact", "counterexample", "inspires")
        service.add_block(
            session,
            {
                "branch_id": main,
                "kind": "text",
                "revision_id": None,
                "body": "阶段结果：原猜想有具体反例。失败路线与精确产物都已保留。"
                "全部研究叙述为预写示例，矩阵运算在本次请求中实际执行，未调用模型。",
            },
        )
        _, branch = service.create_branch(
            session,
            {
                "source_branch_id": main,
                "name": "analytic-labels",
            },
        )
        analytic = branch["branch_id"]
        obj(
            "analytic_question",
            "problem",
            "另一个问题：允许特征值在交叉处交换排序标签后，"
            "是否能选取解析分支？先研究这个具体例子，再单独研究一般情形。",
            branch=analytic,
        )
        obj(
            "analytic_example",
            "claim",
            r"对这个具体 $H(t)$，$\mu_1(t)=t$、$\mu_2(t)=-t$ 是两条解析特征值分支，"
            "但它们不是对所有 $t$ 保持从小到大排序的标签。",
            branch=analytic,
        )
        proof(
            "analytic_argument",
            "analytic_example",
            "固定向量 $(1,1)$ 与 $(1,-1)$ 分别满足 "
            "$H(t)v=tv$ 与 $H(t)w=-tw$；$t$ 与 $-t$ 是多项式，所以这两条分支解析。"
            "$t$ 经过 $0$ 时两条分支的大小关系交换。这里只证明本例，不推广到任意矩阵族。",
            contexts=("sorting",),
            assumptions=("real_parameter",),
            branch=analytic,
        )
        obj(
            "general_question",
            "problem",
            "一般实解析 Hermitian 族能否局部选取解析标签？"
            "待查可靠文献、明确参数维数及假设并建立独立论证；本例不回答一般定理。",
            {"outcome": "open", "requires": ["literature", "independent_general_argument"]},
            branch=analytic,
        )
        relation("analytic_question", "original_question", "rewrites", analytic)
        relation("counterexample", "analytic_question", "inspires", analytic)
        relation("analytic_example", "general_question", "inspires", analytic)
        service.add_block(
            session,
            {
                "branch_id": analytic,
                "kind": "text",
                "revision_id": None,
                "body": "旁支小结：本例存在解析标签 $t$ 与 $-t$，排序标签则不可微。"
                "原问题与反例保留在 main；一般理论仍为独立开放问题。",
            },
        )
        service.emit(
            session,
            project["project_id"],
            main,
            "example.created",
            {
                "source": EXAMPLE_SOURCE,
                "analytic_branch_id": analytic,
                "actual_local_exact_calculation": True,
                "real_model_calls": 0,
            },
            author="example_pre_authored",
        )
        return 201, {
            **project,
            "analytic_branch_id": analytic,
            "objects": named,
            "source": EXAMPLE_SOURCE,
            "pre_authored": True,
            "real_model_calls": 0,
            "computation": result,
        }
