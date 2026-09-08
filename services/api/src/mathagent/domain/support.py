"""Compute branch support under the explicit M1 evidence policy.

These statuses describe recorded support, never mathematical truth.  A current,
adopted claim needs a current, adopted direct proof plan with a nonempty body,
no gaps, and at least one passed human/LLM review, exact computation, or formal
check.  Its evidence must be attached to that argument revision, declare
``coverage == 'whole_plan'``, and describe a nonempty ``scope``.  Any recorded
``issues`` on the claim/argument blocks it pending recheck.  Candidate proofs,
numerical experiments, adoption alone, and partial reviews cannot establish
support.  Evidence provenance and the accuracy of its claimed coverage are the
caller's responsibility; this engine does not execute or verify certificates.

The only unconditional roots are adopted definition/convention contexts and
reviewed direct plans without premises.  Bare claims, even reviewed ones, are
not roots.  Assumption contexts are conditional roots.  Explicit assumptions
may refer to unproved draft/pending claims without upgrading those claims;
they must still be current and neither disputed nor withdrawn.  M1 does not
discharge assumptions or implement induction/contradiction/recursive rules.

All premises of one plan are AND; plans for a conclusion are OR.  A least fixed
point constructs finite derivations from eligible roots, so unsupported cycles
never bootstrap support.  Alternative condition sets are retained internally;
the public result chooses a route with the fewest assumptions, then stable ID
order, then shortest derivation.  ``plan_revision_ids`` includes only plans
matching that chosen condition set and depth.  It does not union assumptions
from alternative routes.  Individual alternatives remain in ``plans``.

Inputs are revision snapshots for one branch, including referenced historical
revisions with ``is_current=False``.  Nothing is mutated or inherited between
versions.  Missing references, stale dependencies, and unsupported rules are
conservatively marked for recheck, not labelled mathematical errors.
"""

from itertools import product
from typing import Any

SUPPORT_POLICY = "m1_recorded_whole_plan_review"
_QUALIFYING_EVIDENCE = {"human_review", "llm_review", "exact_computation", "formal_check"}
_SUCCESS = {"supported", "conditional"}
_CONTEXT_ROLES = {"definition", "assumption", "convention"}
_Conditions = frozenset[str]
_Routes = dict[_Conditions, int]


def _result(status: str, conditions=(), reasons=()) -> dict[str, Any]:
    return {
        "status": status,
        "conditions": sorted(set(conditions)),
        "reasons": list(reasons),
    }


def _context_role(node: dict[str, Any]) -> str | None:
    payload = node.get("payload", {})
    if not isinstance(payload, dict):
        return None
    role = payload.get("role", "assumption")
    return role if isinstance(role, str) else None


def _blocked(node: dict[str, Any], revision_id: str, *, assumption=False):
    """Return a state that prevents this revision from being a support root."""
    adoption = node.get("adoption_state", "draft")
    if adoption == "withdrawn":
        return _result("withdrawn", reasons=[f"{revision_id}: adoption was withdrawn."])
    if not node.get("is_current", False):
        return _result(
            "needs_recheck", reasons=[f"{revision_id}: referenced revision is not current."]
        )
    if adoption == "disputed":
        return _result("needs_recheck", reasons=[f"{revision_id}: adoption is disputed."])
    if any(item.get("verdict") == "issues" for item in node.get("evidence", [])):
        return _result(
            "needs_recheck", reasons=[f"{revision_id}: recorded evidence reports issues."]
        )
    if node.get("kind") == "context" and _context_role(node) not in _CONTEXT_ROLES:
        return _result("needs_recheck", reasons=[f"{revision_id}: context role is invalid."])
    if assumption:
        return None
    if adoption == "draft":
        return _result("draft", reasons=[f"{revision_id}: remains a draft."])
    if adoption != "adopted":
        return _result("needs_recheck", reasons=[f"{revision_id}: has not been adopted."])
    return None


def _has_review(node: dict[str, Any]) -> bool:
    return any(
        item.get("kind") in _QUALIFYING_EVIDENCE
        and item.get("verdict") == "passed"
        and item.get("coverage") == "whole_plan"
        and isinstance(item.get("scope"), str)
        and bool(item["scope"].strip())
        for item in node.get("evidence", [])
    )


def _add_route(routes: _Routes, conditions: _Conditions, depth: int) -> bool:
    """Keep routes undominated in both assumptions and derivation depth.

    A longer route with fewer assumptions cannot discard a shorter route: a
    consumer may already assume the extra conditions and need that shorter
    derivation to retain a concrete, acyclic selected proof.
    """
    if any(
        existing < conditions and existing_depth <= depth
        for existing, existing_depth in routes.items()
    ):
        return False
    previous = routes.get(conditions)
    if previous is not None and previous <= depth:
        return False
    for existing in list(routes):
        if conditions < existing and depth <= routes[existing]:
            del routes[existing]
    routes[conditions] = depth
    return True


def _choose(routes: _Routes) -> tuple[_Conditions, int]:
    return min(routes.items(), key=lambda item: (len(item[0]), sorted(item[0]), item[1]))


def analyze_support(
    nodes: dict[str, dict], plans: list[dict]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return revision-keyed ``claims`` and ``plans`` under ``SUPPORT_POLICY``.

    See the module policy for evidence coverage, adoption, roots, assumptions,
    cycles, and alternative selection.  IDs must be unique revision IDs;
    duplicate plan revision IDs raise ValueError instead of silently overwriting
    an argument.  Context ``payload.role`` defaults to ``assumption``.
    """
    by_id = {plan["revision_id"]: plan for plan in plans}
    if len(by_id) != len(plans):
        raise ValueError("Proof plan revision IDs must be unique.")
    claim_nodes = {key: node for key, node in nodes.items() if node.get("kind") == "claim"}
    claim_blocks = {key: _blocked(node, key) for key, node in claim_nodes.items()}
    plan_blocks: dict[str, dict | None] = {}
    base_conditions: dict[str, _Conditions] = {}
    dependencies: dict[str, list[str]] = {}
    routes: dict[str, _Routes] = {key: {} for key in nodes}

    for key, node in nodes.items():
        if node.get("kind") != "context" or _blocked(node, key):
            continue
        role = _context_role(node)
        if role in {"definition", "convention"}:
            routes[key][frozenset()] = 0
        elif role == "assumption":
            routes[key][frozenset({key})] = 0

    for key, plan in by_id.items():
        assumptions = set(plan.get("assumption_revision_ids", []))
        contexts = list(plan.get("context_revision_ids", []))
        premises = list(plan.get("premise_revision_ids", []))
        conditions = assumptions | {
            ref for ref in contexts if _context_role(nodes.get(ref, {})) == "assumption"
        }
        base_conditions[key] = frozenset(conditions)
        dependencies[key] = sorted((set(premises) | set(contexts)) - assumptions)
        block = None
        argument = nodes.get(key)
        conclusion_id = plan.get("conclusion_revision_id")
        if not argument or argument.get("kind") != "argument":
            block = _result(
                "needs_recheck",
                reasons=[f"{key}: argument revision is missing or has the wrong kind."],
            )
        elif _blocked(argument, key):
            block = _blocked(argument, key)
        elif conclusion_id not in claim_nodes:
            block = _result(
                "needs_recheck", reasons=[f"{key}: conclusion must reference a claim revision."]
            )
        elif claim_blocks[conclusion_id]:
            block = _result(
                "needs_recheck"
                if claim_blocks[conclusion_id]["status"] == "withdrawn"
                else claim_blocks[conclusion_id]["status"],
                reasons=[f"{key}: conclusion is not eligible for current support."]
                + claim_blocks[conclusion_id]["reasons"],
            )
        elif plan.get("rule", "direct") != "direct":
            block = _result(
                "needs_recheck",
                reasons=[
                    f"{key}: inference rule requires explicit review; M1 only evaluates direct plans."
                ],
            )
        elif plan.get("gaps"):
            block = _result("needs_recheck", reasons=[f"{key}: proof plan has unresolved gaps."])
        elif not isinstance(plan.get("body"), str) or not plan["body"].strip():
            block = _result("needs_recheck", reasons=[f"{key}: proof plan has no argument body."])
        elif not _has_review(argument):
            block = _result(
                "needs_recheck",
                reasons=[f"{key}: no passed, scoped whole-plan review satisfies {SUPPORT_POLICY}."],
            )

        # Check every explicitly declared dependency, even if another already
        # prevents support, so invalid assumption declarations cannot be hidden.
        reference_problems = []
        for ref in sorted(set(premises) | set(contexts) | assumptions):
            node = nodes.get(ref)
            if not node:
                reference_problems.append(f"{ref}: referenced revision is missing.")
                continue
            if node.get("kind") not in {"claim", "context"} or (
                ref in contexts and node.get("kind") != "context"
            ):
                reference_problems.append(f"{ref}: invalid mathematical dependency kind.")
                continue
            if not node.get("is_current", False):
                reference_problems.append(f"{ref}: referenced revision is not current.")
            if ref in assumptions:
                assumption_block = _blocked(node, ref, assumption=True)
                if assumption_block:
                    reference_problems.extend(assumption_block["reasons"])
        if reference_problems:
            if block is None:
                block = _result("needs_recheck", reasons=reference_problems)
            else:
                # Adoption remains a separate state: a draft still needs review
                # when its precise dependency versions stop matching the branch.
                if block["status"] != "withdrawn":
                    block["status"] = "needs_recheck"
                block["reasons"].extend(reference_problems)
        if block:
            block["conditions"] = sorted(conditions)
        plan_blocks[key] = block

    def derive(plan_id: str) -> _Routes:
        if plan_blocks[plan_id]:
            return {}
        inputs = [routes[ref] for ref in dependencies[plan_id]]
        if any(not possibilities for possibilities in inputs):
            return {}
        derived: _Routes = {}
        for combination in product(*(possibilities.items() for possibilities in inputs)):
            conditions = base_conditions[plan_id].union(*(item[0] for item in combination))
            depth = 1 + max((item[1] for item in combination), default=0)
            _add_route(derived, conditions, depth)
        return derived

    changed = True
    while changed:
        changed = False
        for key, plan in by_id.items():
            for conditions, depth in derive(key).items():
                if _add_route(routes[plan["conclusion_revision_id"]], conditions, depth):
                    changed = True

    plan_routes = {key: derive(key) for key in by_id}
    claim_results = {}
    for key in claim_nodes:
        if claim_blocks[key]:
            result = dict(claim_blocks[key])
            result["plan_revision_ids"] = []
        elif routes[key]:
            conditions, depth = _choose(routes[key])
            selected = sorted(
                plan_id
                for plan_id, plan in by_id.items()
                if plan["conclusion_revision_id"] == key
                and plan_routes[plan_id].get(conditions) == depth
            )
            result = _result(
                "conditional" if conditions else "supported",
                conditions,
                [
                    f"Finite recorded support under {SUPPORT_POLICY}; this is not a mathematical truth verdict."
                ],
            )
            result["plan_revision_ids"] = selected
        else:
            result = _result(
                "no_current_support",
                reasons=[f"{key}: no eligible finite proof route; adoption alone is not evidence."],
            )
            result["plan_revision_ids"] = []
        claim_results[key] = result

    def dependency_result(ref: str):
        if ref in claim_results:
            return claim_results[ref]
        node = nodes[ref]
        block = _blocked(node, ref)
        if block:
            return block
        return _result(
            "no_current_support", reasons=[f"{ref}: context role is not an eligible root."]
        )

    plan_results = {}
    for key in by_id:
        if plan_blocks[key]:
            plan_results[key] = dict(plan_blocks[key])
        elif plan_routes[key]:
            conditions, _ = _choose(plan_routes[key])
            plan_results[key] = _result(
                "conditional" if conditions else "supported",
                conditions,
                [
                    f"All declared premises have finite support under {SUPPORT_POLICY}; unresolved assumptions remain conditions."
                ],
            )
        else:
            plan_results[key] = _result("no_current_support", base_conditions[key])

    # Propagate recheck only through unavailable routes.  A good OR alternative
    # keeps its conclusion usable and stops invalidation reaching its consumers.
    changed = True
    while changed:
        changed = False
        for key in by_id:
            result = plan_results[key]
            if plan_blocks[key] or result["status"] in _SUCCESS:
                continue
            failures = [
                (ref, dependency_result(ref)) for ref in dependencies[key] if not routes[ref]
            ]
            status = (
                "needs_recheck"
                if any(item["status"] == "needs_recheck" for _, item in failures)
                else "no_current_support"
            )
            if result["status"] != status:
                result["status"] = status
                changed = True
            result["reasons"] = [
                f"{ref}: premise/context has {item['status']}; no eligible finite support route (unsupported cycles cannot establish support)."
                for ref, item in failures
            ]
        for key, result in claim_results.items():
            if claim_blocks[key] or result["status"] in _SUCCESS:
                continue
            invalid = sorted(
                plan_id
                for plan_id, plan in by_id.items()
                if plan["conclusion_revision_id"] == key
                and plan_results[plan_id]["status"] == "needs_recheck"
            )
            if invalid and result["status"] != "needs_recheck":
                result["status"] = "needs_recheck"
                result["reasons"] = [
                    f"{key}: no usable alternative; proof plans require recheck: {', '.join(invalid)}."
                ]
                changed = True

    return {"claims": claim_results, "plans": plan_results}
