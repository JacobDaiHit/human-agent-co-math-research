"""Explicit branch merge planning keeps mathematical choices human-reviewable."""

import pytest
from mathagent.application.branch_merge import BranchMergeService
from mathagent.application.errors import DomainError
from mathagent.persistence.models import RevisionParent
from sqlalchemy import select
from test_autonomous_agent import app as upstream_app
from test_autonomous_agent import exercise, project_and_run

app_fixture = upstream_app


async def branches_with_conflicting_context(api):
    project, _ = await project_and_run(api, autonomous=False)
    context = await api.write("/objects", {
        "branch_id": project["branch_id"], "kind": "context", "body": "Let $G$ be finite.",
        "payload": {"role": "definition"},
    })
    source = await api.write("/branches", {"source_branch_id": project["branch_id"], "name": "source"})
    target = await api.write("/branches", {"source_branch_id": project["branch_id"], "name": "target"})
    source_revision = await api.write(f"/objects/{context['object_id']}/revisions", {
        "branch_id": source["branch_id"], "expected_revision_id": context["revision_id"],
        "body": "Let $G$ be finite.", "payload": {"role": "assumption"},
    })
    target_revision = await api.write(f"/objects/{context['object_id']}/revisions", {
        "branch_id": target["branch_id"], "expected_revision_id": context["revision_id"],
        "body": "Let $G$ be finite.", "payload": {"role": "definition"},
    })
    return project, context, source, target, source_revision, target_revision


def test_preview_requires_human_resolution_for_equal_text_definition_conflict(app_fixture):
    async def scenario(api, _client):
        _, context, source, target, _, _ = await branches_with_conflicting_context(api)
        with app_fixture.state.database.sessions() as session:
            _, preview = BranchMergeService(app_fixture.state.service).preview(session, {
                "source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"],
            })
            item = next(value for value in preview["items"] if value["object_id"] == context["object_id"])
            assert item["status"] == "diverged_requires_resolution"
            assert item["semantic_review_required"] is True
            assert item["source_body"] == item["target_body"] == "Let $G$ be finite."
            assert item["ancestor_body"] == "Let $G$ be finite."
            with pytest.raises(DomainError, match="明确人工决议"):
                BranchMergeService(app_fixture.state.service).apply(session, {
                    "source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"],
                    "preview_token": preview["preview_token"], "resolutions": [],
                })
            with pytest.raises(DomainError, match="重复合并决议"):
                BranchMergeService(app_fixture.state.service).apply(session, {
                    "source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"],
                    "preview_token": preview["preview_token"], "resolutions": [
                        {"object_id": context["object_id"], "choice": "source", "reason": "Review it."},
                        {"object_id": context["object_id"], "choice": "target", "reason": "Keep it."},
                    ],
                })
    exercise(app_fixture, scenario)


def test_apply_rewrite_records_both_parents_without_migrating_old_review(app_fixture):
    async def scenario(api, _client):
        project, context, source, target, source_revision, target_revision = await branches_with_conflicting_context(api)
        await api.write("/reviews", {"branch_id": target["branch_id"],
            "target_revision_id": target_revision["revision_id"], "kind": "human_review", "verdict": "passed",
            "coverage": "whole_plan", "scope": "Old definition only", "findings": ["Old version reviewed."],
        })
        with app_fixture.state.database.sessions.begin() as session:
            service = BranchMergeService(app_fixture.state.service)
            _, preview = service.preview(session, {"source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"]})
            _, applied = service.apply(session, {"source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"],
                "preview_token": preview["preview_token"], "resolutions": [{"object_id": context["object_id"],
                "choice": "rewrite", "body": "Assume $G$ is finite for this argument.",
                "payload": {"role": "assumption"}, "reason": "Definitions and assumptions have different roles."}],
            })
            merged = applied["changes"][0]["revision_id"]
            assert set(session.scalars(select(RevisionParent.parent_id).where(
                RevisionParent.revision_id == merged))) == {source_revision["revision_id"], target_revision["revision_id"]}
            assert applied["reviews_migrated"] is False and applied["adoptions_migrated"] is False
        snapshot = await api.get(f"/projects/{project['project_id']}/snapshot",)
        assert not any(review["target_revision_id"] == merged for review in snapshot["reviews"])
    exercise(app_fixture, scenario)


def test_apply_rejects_stale_preview_and_cross_project_branches(app_fixture):
    async def scenario(api, _client):
        project, context, source, target, _, target_revision = await branches_with_conflicting_context(api)
        with app_fixture.state.database.sessions() as session:
            service = BranchMergeService(app_fixture.state.service)
            _, preview = service.preview(session, {"source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"]})
        await api.write(f"/objects/{context['object_id']}/revisions", {"branch_id": target["branch_id"],
            "expected_revision_id": target_revision["revision_id"], "body": "Let $G$ be a finite group."})
        with app_fixture.state.database.sessions() as session:
            service = BranchMergeService(app_fixture.state.service)
            with pytest.raises(DomainError, match="重新预览"):
                service.apply(session, {"source_branch_id": source["branch_id"], "target_branch_id": target["branch_id"],
                    "preview_token": preview["preview_token"], "resolutions": []})
        other, _ = await project_and_run(api, autonomous=False)
        with app_fixture.state.database.sessions() as session:
            with pytest.raises(DomainError, match="同一项目"):
                BranchMergeService(app_fixture.state.service).preview(session, {
                    "source_branch_id": source["branch_id"], "target_branch_id": other["branch_id"],
                })
        assert project["project_id"] != other["project_id"]
    exercise(app_fixture, scenario)
