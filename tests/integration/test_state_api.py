"""State contract acceptance against the HTTP API and an actual SQLite database.

These scenarios deliberately create evidence through public commands: adopting a
claim alone must never manufacture mathematical support.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@dataclass
class StateAPI:
    client: TestClient
    project_id: str
    branch_id: str

    def command(self, path: str, payload: dict, *, key: str | None = None):
        return self.client.post(
            path,
            json=payload,
            headers={**AUTH, "Idempotency-Key": key or str(uuid4())},
        )

    def post(self, path: str, payload: dict) -> dict:
        response = self.command(path, payload)
        assert response.status_code in (200, 201), response.text
        return response.json()

    def snapshot(self, branch_id: str | None = None) -> dict:
        response = self.client.get(
            f"/projects/{self.project_id}/snapshot",
            params={"branch_id": branch_id or self.branch_id},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def object(self, body: str, kind: str = "claim", **payload) -> dict:
        return self.post(
            "/objects",
            {"branch_id": self.branch_id, "kind": kind, "body": body, "payload": payload},
        )

    def adopt(self, revision_id: str, state: str = "adopted") -> dict:
        return self.post(
            "/adoptions",
            {
                "branch_id": self.branch_id,
                "revision_id": revision_id,
                "state": state,
                "reason": f"Researcher explicitly sets {state} for this exact revision.",
            },
        )

    def review(self, revision_id: str, **overrides) -> dict:
        return self.post(
            "/reviews",
            {
                "branch_id": self.branch_id,
                "target_revision_id": revision_id,
                "kind": "human_review",
                "verdict": "passed",
                "coverage": "whole_plan",
                "scope": "All declared premises, inference steps, and the conclusion.",
                "findings": ["The stated inference is valid under the declared dependencies."],
                **overrides,
            },
        )

    def plan(self, conclusion: dict, premises: tuple[dict, ...] = (), **overrides) -> dict:
        return self.post(
            "/proof-plans",
            {
                "branch_id": self.branch_id,
                "conclusion_revision_id": conclusion["revision_id"],
                "premise_revision_ids": [p["revision_id"] for p in premises],
                "body": "Explicit test argument with every dependency declared.",
                **overrides,
            },
        )

    def accepted_plan(self, conclusion: dict, premises: tuple[dict, ...] = (), **kwargs):
        plan = self.plan(conclusion, premises, **kwargs)
        self.review(plan["revision_id"])
        self.adopt(plan["revision_id"])
        self.adopt(conclusion["revision_id"])
        return plan

    def root(self, statement: str) -> dict:
        claim = self.object(statement)
        self.accepted_plan(claim, body=f"A complete premise-free proof of: {statement}")
        return claim

    def revise(self, obj: dict, body: str, **overrides) -> dict:
        return self.post(
            f"/objects/{obj['object_id']}/revisions",
            {
                "branch_id": self.branch_id,
                "expected_revision_id": obj["revision_id"],
                "body": body,
                **overrides,
            },
        )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "research.sqlite3"


@pytest.fixture
def api(database_path: Path):
    with TestClient(create_app(database_path=database_path, token=TOKEN)) as client:
        response = client.post(
            "/projects",
            json={"title": "Version-aware mathematical research", "body": "Study a theorem."},
            headers={**AUTH, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code in (200, 201), response.text
        created = response.json()
        yield StateAPI(client, created["project_id"], created["branch_id"])


def claim_status(snapshot: dict, claim: dict) -> str:
    return snapshot["support"]["claims"][claim["revision_id"]]["status"]


def plan_status(snapshot: dict, plan: dict) -> str:
    return snapshot["support"]["plans"][plan["revision_id"]]["status"]


def head(snapshot: dict, object_id: str) -> dict:
    return next(obj["revision"] for obj in snapshot["objects"] if obj["id"] == object_id)


def test_revision_cas_preserves_history_and_idempotent_conflict_receipt(api: StateAPI):
    lemma = api.object("Lemma version one", source="handwritten")
    updated = api.revise(lemma, "Lemma version two", payload={"source": "corrected"})
    key = str(uuid4())
    stale = {
        "branch_id": api.branch_id,
        "expected_revision_id": lemma["revision_id"],
        "body": "A worker's competing version",
    }
    conflict = api.command(f"/objects/{lemma['object_id']}/revisions", stale, key=key)
    assert conflict.status_code == 409, conflict.text
    receipt = conflict.json()
    assert receipt["current_revision_id"] == updated["revision_id"]
    assert receipt["candidate_revision_id"] not in (lemma["revision_id"], updated["revision_id"])

    newest = api.revise(updated, "Lemma version three")
    replay = api.command(f"/objects/{lemma['object_id']}/revisions", stale, key=key)
    assert replay.status_code == 409
    assert replay.json() == receipt
    snapshot = api.snapshot()
    assert head(snapshot, lemma["object_id"])["id"] == newest["revision_id"]
    assert len(snapshot["conflicts"]) == 1

    history = api.client.get(f"/objects/{lemma['object_id']}/revisions", headers=AUTH)
    assert history.status_code == 200
    revisions = {revision["id"]: revision for revision in history.json()["revisions"]}
    assert len(revisions) == 4
    assert revisions[lemma["revision_id"]]["body"] == "Lemma version one"
    assert revisions[lemma["revision_id"]]["payload"] == {"source": "handwritten"}
    assert revisions[updated["revision_id"]]["body"] == "Lemma version two"
    assert revisions[receipt["candidate_revision_id"]]["body"] == stale["body"]


def test_idempotent_commands_do_not_duplicate_objects_or_events(api: StateAPI):
    key = str(uuid4())
    payload = {"branch_id": api.branch_id, "kind": "context", "body": "H is Hermitian."}
    first = api.command("/objects", payload, key=key)
    assert first.status_code in (200, 201), first.text
    before = api.snapshot()
    repeated = api.command("/objects", payload, key=key)
    assert repeated.status_code == first.status_code
    assert repeated.json() == first.json()
    assert len(api.snapshot()["objects"]) == len(before["objects"])
    changed = api.command("/objects", {**payload, "body": "Changed command"}, key=key)
    assert changed.status_code == 409, changed.text

    events = api.client.get(f"/projects/{api.project_id}/events", headers=AUTH).json()
    seqs = [event["seq"] for event in events["events"]]
    assert seqs == list(range(1, events["last_seq"] + 1))
    assert len(seqs) == 2  # One project command and one object command.
    tail = api.client.get(
        f"/projects/{api.project_id}/events", params={"after_seq": seqs[0]}, headers=AUTH
    ).json()
    assert [event["seq"] for event in tail["events"]] == seqs[1:]
    assert tail["last_seq"] == events["last_seq"]


def test_stale_read_set_keeps_candidate_without_overwriting_unchanged_target(api: StateAPI):
    premise = api.object("A v1")
    conclusion = api.object("B v1")
    api.revise(premise, "A v2")
    response = api.command(
        f"/objects/{conclusion['object_id']}/revisions",
        {
            "branch_id": api.branch_id,
            "expected_revision_id": conclusion["revision_id"],
            "body": "B produced from stale A",
            "read_set": {premise["object_id"]: premise["revision_id"]},
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["candidate_revision_id"] != conclusion["revision_id"]
    assert head(api.snapshot(), conclusion["object_id"])["id"] == conclusion["revision_id"]


def test_competing_writers_preserve_one_head_and_every_candidate(api: StateAPI):
    lemma = api.object("The shared base revision")

    def write(index: int):
        return api.command(
            f"/objects/{lemma['object_id']}/revisions",
            {
                "branch_id": api.branch_id,
                "expected_revision_id": lemma["revision_id"],
                "body": f"Independent candidate {index}",
            },
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(write, range(3)))
    assert sorted(response.status_code for response in responses) == [201, 409, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    snapshot = api.snapshot()
    assert head(snapshot, lemma["object_id"])["id"] == winner["revision_id"]
    assert len(snapshot["conflicts"]) == 2
    history = api.client.get(f"/objects/{lemma['object_id']}/revisions", headers=AUTH).json()
    assert len(history["revisions"]) == 4
    candidates = [
        revision for revision in history["revisions"] if revision["id"] != lemma["revision_id"]
    ]
    assert {revision["body"] for revision in candidates} == {
        f"Independent candidate {index}" for index in range(3)
    }
    assert all(revision["parent_revision_ids"] == [lemma["revision_id"]] for revision in candidates)


def test_adoption_is_separate_from_evidence_and_both_plan_and_claim_are_required(api: StateAPI):
    claim = api.object("A proposition accepted provisionally by the researcher")
    api.adopt(claim["revision_id"])
    assert claim_status(api.snapshot(), claim) != "supported"
    plan = api.plan(claim)
    api.review(plan["revision_id"])
    assert claim_status(api.snapshot(), claim) != "supported"
    api.adopt(plan["revision_id"])
    assert claim_status(api.snapshot(), claim) == "supported"
    api.adopt(claim["revision_id"], "withdrawn")
    assert claim_status(api.snapshot(), claim) != "supported"


@pytest.mark.parametrize(
    "review_fields",
    [
        {"kind": "candidate_proof"},
        {"kind": "numerical_experiment"},
        {"kind": "exact_computation", "coverage": "partial", "scope": "One matrix only."},
        {"coverage": "partial"},
        {"verdict": "issues"},
        {"verdict": "inconclusive"},
    ],
)
def test_limited_evidence_never_promotes_general_proof(api: StateAPI, review_fields: dict):
    claim = api.object("Every matrix in this class has property P")
    plan = api.plan(claim)
    api.adopt(claim["revision_id"])
    api.adopt(plan["revision_id"])
    api.review(plan["revision_id"], **review_fields)
    assert claim_status(api.snapshot(), claim) != "supported"
    assert plan_status(api.snapshot(), plan) != "supported"


@pytest.mark.parametrize("kind", ["llm_review", "formal_check", "exact_computation"])
def test_unavailable_review_tools_cannot_be_fabricated(api: StateAPI, kind: str):
    claim = api.object("A claim with no actual tool result")
    plan = api.plan(claim)
    response = api.command(
        "/reviews",
        {
            "branch_id": api.branch_id,
            "target_revision_id": plan["revision_id"],
            "kind": kind,
            "verdict": "passed",
            "coverage": "whole_plan",
            "scope": "An unsupported assertion of tool execution.",
            "findings": ["An unsupported assertion of success."],
        },
    )
    assert response.status_code == 422, response.text
    assert not api.snapshot()["reviews"]


def test_and_premises_alternative_proofs_and_transitive_invalidation(api: StateAPI):
    a, b, d = (api.root(name) for name in ("A", "B", "D"))
    c, e = api.object("C"), api.object("E")
    p1 = api.accepted_plan(c, (a, b))
    p2 = api.accepted_plan(c, (d,))
    p3 = api.accepted_plan(e, (c,))
    initial = api.snapshot()
    assert claim_status(initial, c) == claim_status(initial, e) == "supported"

    api.adopt(b["revision_id"], "withdrawn")
    alternative = api.snapshot()
    assert plan_status(alternative, p1) != "supported"
    assert plan_status(alternative, p2) == "supported"
    assert claim_status(alternative, c) == claim_status(alternative, e) == "supported"

    api.adopt(d["revision_id"], "withdrawn")
    invalidated = api.snapshot()
    assert claim_status(invalidated, c) != "supported"
    assert claim_status(invalidated, e) != "supported"
    assert plan_status(invalidated, p3) != "supported"
    c_object = next(obj for obj in invalidated["objects"] if obj["id"] == c["object_id"])
    assert c_object["adoption_state"] == "adopted"  # Lost support is not a refutation.

    api.adopt(b["revision_id"])
    restored = api.snapshot()
    assert claim_status(restored, c) == claim_status(restored, e) == "supported"


def test_withdrawing_one_proof_preserves_independent_alternative(api: StateAPI):
    a, b = api.root("A"), api.root("B")
    c = api.object("C")
    p1, p2 = api.accepted_plan(c, (a,)), api.accepted_plan(c, (b,))
    api.adopt(p1["revision_id"], "withdrawn")
    snapshot = api.snapshot()
    assert plan_status(snapshot, p1) != "supported"
    assert plan_status(snapshot, p2) == "supported"
    assert claim_status(snapshot, c) == "supported"


def test_revision_impact_stops_at_a_still_supported_alternative(api: StateAPI):
    a, b = api.root("A version one"), api.root("Independent B")
    c, d = api.object("C"), api.object("D derived from C")
    affected_proof = api.accepted_plan(c, (a,))
    alternative = api.accepted_plan(c, (b,))
    downstream = api.accepted_plan(d, (c,))
    receipt = api.revise(a, "A version two, not yet proved")
    assert affected_proof["revision_id"] in receipt["affected_plan_revision_ids"]
    assert not {alternative["revision_id"], downstream["revision_id"]}.intersection(
        receipt["affected_plan_revision_ids"]
    )
    snapshot = api.snapshot()
    assert plan_status(snapshot, affected_proof) == "needs_recheck"
    assert plan_status(snapshot, alternative) == "supported"
    assert claim_status(snapshot, c) == claim_status(snapshot, d) == "supported"


def test_changed_context_invalidates_only_dependent_proof_and_keeps_old_review(api: StateAPI):
    context = api.object("Work over the real numbers", kind="context", role="definition")
    api.adopt(context["revision_id"])
    claim, child = api.object("L"), api.object("L implies T")
    proof = api.accepted_plan(claim, context_revision_ids=[context["revision_id"]])
    downstream = api.accepted_plan(child, (claim,))
    before = api.snapshot()
    old_reviews = {review["id"]: review for review in before["reviews"]}
    assert claim_status(before, claim) == claim_status(before, child) == "supported"
    changed = api.revise(context, "Work over the complex numbers")
    assert {proof["revision_id"], downstream["revision_id"]}.issubset(
        changed["affected_plan_revision_ids"]
    )
    after = api.snapshot()
    assert claim_status(after, claim) != "supported"
    assert claim_status(after, child) != "supported"
    assert {review["id"]: review for review in after["reviews"]} == old_reviews


def test_revising_argument_keeps_structure_but_requires_review_of_new_version(api: StateAPI):
    premise = api.root("Reviewed premise")
    claim = api.object("Conclusion")
    argument = api.accepted_plan(claim, (premise,))
    old_reviews = api.snapshot()["reviews"]
    revised = api.revise(argument, "A substantially changed inference step")
    api.adopt(revised["revision_id"])
    snapshot = api.snapshot()
    assert claim_status(snapshot, claim) != "supported"
    assert plan_status(snapshot, revised) != "supported"
    plan = next(p for p in snapshot["proof_plans"] if p["revision_id"] == revised["revision_id"])
    assert plan["premise_revision_ids"] == [premise["revision_id"]]
    assert plan["conclusion_revision_id"] == claim["revision_id"]
    assert snapshot["reviews"] == old_reviews
    api.review(revised["revision_id"])
    assert claim_status(api.snapshot(), claim) == "supported"


def test_new_claim_revision_does_not_inherit_support_or_review(api: StateAPI):
    claim = api.root("The original precisely stated lemma")
    assert claim_status(api.snapshot(), claim) == "supported"
    replacement = api.revise(claim, "A stronger, unproved version of the lemma")
    api.adopt(replacement["revision_id"])
    snapshot = api.snapshot()
    assert claim_status(snapshot, replacement) != "supported"
    assert all(
        review["target_revision_id"] != replacement["revision_id"] for review in snapshot["reviews"]
    )


def test_unproved_assumptions_are_conditional_and_circular_proofs_do_not_bootstrap(api: StateAPI):
    q, lemma = api.object("Unproved Q"), api.object("L follows if Q holds")
    conditional = api.accepted_plan(lemma, assumption_revision_ids=[q["revision_id"]])
    snapshot = api.snapshot()
    assert claim_status(snapshot, lemma) == "conditional"
    assert plan_status(snapshot, conditional) == "conditional"
    assert snapshot["support"]["claims"][lemma["revision_id"]]["conditions"] == [q["revision_id"]]
    child = api.object("A consequence of the conditional lemma")
    api.accepted_plan(child, (lemma,))
    propagated = api.snapshot()["support"]["claims"][child["revision_id"]]
    assert propagated["status"] == "conditional"
    assert propagated["conditions"] == [q["revision_id"]]

    a, b = api.object("Circular A"), api.object("Circular B")
    api.accepted_plan(a, (b,))
    api.accepted_plan(b, (a,))
    snapshot = api.snapshot()
    assert claim_status(snapshot, a) != "supported"
    assert claim_status(snapshot, b) != "supported"
    for source, target in ((a, b), (b, a)):
        api.post(
            "/relations",
            {
                "branch_id": api.branch_id,
                "source_id": source["object_id"],
                "target_id": target["object_id"],
                "kind": "inspires",
            },
        )


def test_explicit_gap_prevents_complete_support(api: StateAPI):
    claim = api.object("Convergence of the proposed iteration")
    plan = api.accepted_plan(claim, gaps=["Uniform convergence is unproved."])
    snapshot = api.snapshot()
    assert claim_status(snapshot, claim) != "supported"
    assert plan_status(snapshot, plan) != "supported"


def test_branch_heads_and_adoption_decisions_are_isolated(api: StateAPI):
    original = api.root("A lemma shared at the fork point")
    fork = api.post("/branches", {"source_branch_id": api.branch_id, "name": "alternative"})
    branch_id = fork["branch_id"]
    assert claim_status(api.snapshot(branch_id), original) == "supported"
    replacement = api.revise(original, "Main branch revised lemma")
    assert head(api.snapshot(), original["object_id"])["id"] == replacement["revision_id"]
    assert head(api.snapshot(branch_id), original["object_id"])["id"] == original["revision_id"]
    assert claim_status(api.snapshot(branch_id), original) == "supported"
    api.post(
        "/adoptions",
        {
            "branch_id": branch_id,
            "revision_id": original["revision_id"],
            "state": "withdrawn",
            "reason": "Withdraw only in the alternative branch.",
        },
    )
    assert claim_status(api.snapshot(branch_id), original) != "supported"
    assert head(api.snapshot(), original["object_id"])["id"] == replacement["revision_id"]


def test_cross_project_references_are_rejected_without_partial_writes(api: StateAPI):
    local = api.object("Local claim")
    foreign = api.post("/projects", {"title": "Private separate project", "body": "Other goal"})
    remote_api = StateAPI(api.client, foreign["project_id"], foreign["branch_id"])
    remote = remote_api.object("Foreign claim")
    before = api.snapshot()
    bad_commands = [
        ("/proof-plans", {"conclusion_revision_id": remote["revision_id"], "body": "Bad"}),
        (
            "/proof-plans",
            {
                "conclusion_revision_id": local["revision_id"],
                "premise_revision_ids": [remote["revision_id"]],
                "body": "Bad dependency",
            },
        ),
        (
            "/adoptions",
            {"revision_id": remote["revision_id"], "state": "adopted", "reason": "Bad"},
        ),
        (
            "/reviews",
            {
                "target_revision_id": remote["revision_id"],
                "kind": "human_review",
                "verdict": "passed",
                "coverage": "whole_plan",
                "scope": "Bad",
                "findings": ["Bad"],
            },
        ),
        (
            "/relations",
            {"source_id": local["object_id"], "target_id": remote["object_id"], "kind": "similar"},
        ),
        ("/manuscript/blocks", {"kind": "reference", "revision_id": remote["revision_id"]}),
        (
            f"/objects/{remote['object_id']}/revisions",
            {"expected_revision_id": remote["revision_id"], "body": "Foreign write"},
        ),
    ]
    for path, payload in bad_commands:
        response = api.command(path, {"branch_id": api.branch_id, **payload})
        assert response.status_code in (400, 404, 409, 422), (path, response.text)
    after = api.snapshot()
    for key in ("objects", "proof_plans", "reviews", "manuscript", "conflicts"):
        assert after[key] == before[key]
    assert head(remote_api.snapshot(), remote["object_id"])["id"] == remote["revision_id"]


def test_state_manuscript_and_idempotency_receipts_survive_reopening(database_path: Path):
    key = str(uuid4())
    request = {"title": "Persistent project", "body": "Original research question"}
    with TestClient(create_app(database_path=database_path, token=TOKEN)) as client:
        created = client.post("/projects", json=request, headers={**AUTH, "Idempotency-Key": key})
        assert created.status_code in (200, 201), created.text
        result = created.json()
        state = StateAPI(client, result["project_id"], result["branch_id"])
        context = state.object("Definitions and hypotheses", kind="context")
        lemma = state.root("A preserved lemma")
        state.post(
            "/manuscript/blocks",
            {"branch_id": state.branch_id, "kind": "text", "body": "An unfinished draft."},
        )
        state.post(
            "/manuscript/blocks",
            {
                "branch_id": state.branch_id,
                "kind": "reference",
                "revision_id": lemma["revision_id"],
            },
        )
        before = state.snapshot()
    assert database_path.exists()
    with TestClient(create_app(database_path=database_path, token=TOKEN)) as client:
        reopened = StateAPI(client, result["project_id"], result["branch_id"])
        after = reopened.snapshot()
        assert after == before
        assert head(after, context["object_id"])["body"] == "Definitions and hypotheses"
        assert claim_status(after, lemma) == "supported"
        assert [block["kind"] for block in after["manuscript"][-2:]] == ["text", "reference"]
        assert after["manuscript"][-2]["body"] == "An unfinished draft."
        assert after["manuscript"][-1]["revision_id"] == lemma["revision_id"]
        replay = client.post("/projects", json=request, headers={**AUTH, "Idempotency-Key": key})
        assert replay.status_code == created.status_code
        assert replay.json() == result


def test_read_endpoints_require_token_and_reject_foreign_origin(api: StateAPI):
    claim = api.object("Sensitive private research")
    paths = [
        "/projects",
        f"/projects/{api.project_id}/snapshot?branch_id={api.branch_id}",
        f"/projects/{api.project_id}/events?after_seq=0",
        f"/objects/{claim['object_id']}/revisions",
    ]
    for path in paths:
        missing = api.client.get(path)
        assert missing.status_code == 401, (path, missing.text)
        invalid = api.client.get(path, headers={"Authorization": "Bearer wrong-token"})
        assert invalid.status_code == 401, (path, invalid.text)
        foreign = api.client.get(path, headers={**AUTH, "Origin": "https://untrusted.example"})
        assert foreign.status_code == 403, (path, foreign.text)
        for origin in ("http://localhost:8000", "http://127.0.0.1:8000"):
            allowed = api.client.get(path, headers={**AUTH, "Origin": origin})
            assert allowed.status_code == 200, (path, allowed.text)


def test_write_rejects_missing_idempotency_key_and_foreign_origin(api: StateAPI):
    payload = {"branch_id": api.branch_id, "kind": "claim", "body": "An attempted mutation"}
    before = api.snapshot()
    no_key = api.client.post("/objects", json=payload, headers=AUTH)
    assert no_key.status_code in (400, 422), no_key.text
    foreign = api.client.post(
        "/objects",
        json=payload,
        headers={**AUTH, "Idempotency-Key": str(uuid4()), "Origin": "https://untrusted.example"},
    )
    assert foreign.status_code == 403, foreign.text
    assert api.snapshot()["objects"] == before["objects"]
