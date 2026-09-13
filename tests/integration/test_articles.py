"""Article drafts retain fixed source versions and their unresolved evidence."""

import pytest
from mathagent.application.articles import ArticleService
from mathagent.application.errors import DomainError
from test_autonomous_agent import app as upstream_app
from test_autonomous_agent import exercise, project_and_run

app_fixture = upstream_app


def test_revised_article_keeps_fixed_sources_in_export_and_review_context(app_fixture):
    async def scenario(api, _client):
        project, _ = await project_and_run(api, autonomous=False)
        source = await api.write("/objects", {"branch_id": project["branch_id"], "kind": "claim", "body": "Pinned old source."})
        article = await api.write("/articles", {"branch_id": project["branch_id"], "title": "Pinned article",
            "sections": [{"heading": "Argument", "revision_ids": [source["revision_id"]]}]})
        revised = await api.write(f"/objects/{article['object_id']}/revisions", {
            "branch_id": project["branch_id"], "expected_revision_id": article["revision_id"], "body": "Edited article text."})
        await api.write(f"/objects/{source['object_id']}/revisions", {
            "branch_id": project["branch_id"], "expected_revision_id": source["revision_id"], "body": "Changed source."})
        review = await api.write("/runs", {"branch_id": project["branch_id"], "goal_object_id": article["object_id"],
            "mode": "review", "provider": "fake", "autonomous": False})
        task = await api.write(f"/runs/{review['run_id']}/claim", worker=True)
        inputs = {item["revision_id"]: item for item in task["inputs"]}
        assert inputs[source["revision_id"]]["body"] == "Pinned old source."
        assert revised["revision_id"] in inputs
        from mathagent.exports.service import ExportService
        with app_fixture.state.database.sessions.begin() as session:
            _, exported = ExportService(app_fixture.state.service).create(session, {
                "project_id": project["project_id"], "branch_id": project["branch_id"], "object_ids": [article["object_id"]]})
            assert source["revision_id"] in {rev["id"] for rev in exported["bundle"]["revisions"]}
    exercise(app_fixture, scenario)


def test_article_pins_sections_and_requires_its_own_review(app_fixture):
    async def scenario(api, _client):
        project, _ = await project_and_run(api, autonomous=False)
        assumption = await api.write("/objects", {"branch_id": project["branch_id"], "kind": "context",
            "body": "Assume $n>0$.", "payload": {"role": "assumption"}})
        claim = await api.write("/objects", {"branch_id": project["branch_id"], "kind": "claim",
            "body": "Then $n^2>0$."})
        with app_fixture.state.database.sessions.begin() as session:
            _, article = ArticleService(app_fixture.state.service).create(session, {"branch_id": project["branch_id"],
                "title": "A short positivity argument", "sections": [
                    {"heading": "Assumption", "revision_ids": [assumption["revision_id"]], "transition": "We fix the hypothesis."},
                    {"heading": "Claim", "revision_ids": [claim["revision_id"]], "transition": "It now implies the conclusion."},
                ]})
            assert article["issues"][-1]["code"] == "article_review_required"
            article_id = article["revision_id"]
        with app_fixture.state.database.sessions() as session:
            _, checked = ArticleService(app_fixture.state.service).check(session, {"branch_id": project["branch_id"], "revision_id": article_id})
            assert any(issue["code"] == "source_unreviewed" for issue in checked["issues"])
            assert any(issue["code"] == "article_review_required" for issue in checked["issues"])
            assert checked["mathematical_semantics_verified"] is False
        for revision_id in (project["revision_id"], assumption["revision_id"], claim["revision_id"]):
            await api.write("/reviews", {"branch_id": project["branch_id"], "target_revision_id": revision_id,
                "kind": "human_review", "verdict": "passed", "coverage": "partial",
                "scope": "Synthetic source review.", "findings": ["Reviewed source."],
            })
        with app_fixture.state.database.sessions() as session:
            _, checked = ArticleService(app_fixture.state.service).check(session, {"branch_id": project["branch_id"], "revision_id": article_id})
            assert not any(issue["code"] == "source_unreviewed" for issue in checked["current_issues"])
            assert any(issue["code"] == "source_unreviewed" for issue in checked["creation_issues"])
    exercise(app_fixture, scenario)


def test_article_rejects_foreign_source_and_reports_changed_source_version(app_fixture):
    async def scenario(api, _client):
        project, _ = await project_and_run(api, autonomous=False)
        source = await api.write("/objects", {"branch_id": project["branch_id"], "kind": "claim", "body": "Claim v1."})
        foreign, _ = await project_and_run(api, autonomous=False)
        foreign_source = await api.write("/objects", {"branch_id": foreign["branch_id"], "kind": "claim", "body": "Private."})
        with app_fixture.state.database.sessions() as session:
            service = ArticleService(app_fixture.state.service)
            with pytest.raises(DomainError):
                service.create(session, {"branch_id": project["branch_id"], "title": "No leak", "sections": [
                    {"heading": "Foreign", "revision_ids": [foreign_source["revision_id"]], "transition": ""},
                ]})
        with app_fixture.state.database.sessions.begin() as session:
            _, article = ArticleService(app_fixture.state.service).create(session, {"branch_id": project["branch_id"],
                "title": "Pinned", "sections": [{"heading": "Claim", "revision_ids": [project["revision_id"], source["revision_id"]], "transition": ""}]})
        await api.write(f"/objects/{source['object_id']}/revisions", {"branch_id": project["branch_id"],
            "expected_revision_id": source["revision_id"], "body": "Claim v2."})
        with app_fixture.state.database.sessions() as session:
            _, checked = ArticleService(app_fixture.state.service).check(session, {"branch_id": project["branch_id"], "revision_id": article["revision_id"]})
            assert any(issue["code"] == "source_version_changed" for issue in checked["issues"])
    exercise(app_fixture, scenario)
