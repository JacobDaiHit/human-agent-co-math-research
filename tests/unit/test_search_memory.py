from mathagent.application.deletion import DeletionService
from mathagent.application.state import StateService
from mathagent.exports.service import ExportService
from mathagent.persistence.database import Database
from mathagent.persistence.models import Revision, Run
from mathagent.persistence.search_models import (
    SearchDecision,
    SearchMemory,
    SearchRoute,
    SearchSession,
    SearchWork,
)
from mathagent.runtime.memory import MemoryService
from sqlalchemy import select


def make_search(tmp_path):
    database = Database(tmp_path / "memory.sqlite3")
    database.migrate()
    state = StateService(database)
    with database.sessions.begin() as session:
        _, created = state.create_project(session, {"title": "Memory", "body": "Show P"})
        root = Run(branch_id=created["branch_id"], goal_object_id=created["object_id"])
        session.add(root)
        session.flush()
        session.add(SearchSession(root_run_id=root.id, project_id=created["project_id"],
            goal_revision_id=created["revision_id"], config={"initial_read_set": {
                created["object_id"]: created["revision_id"]}}))
        route_branch = state.clone_branch(session, state.require_branch(session, created["branch_id"]), "route")
        _, card_revision = state.new_object(session, route_branch, "note", "route card", {}, "test")
        route = SearchRoute(root_run_id=root.id, branch_id=route_branch.id,
            card_revision_id=card_revision.id, ordinal=0)
        session.add(route)
        session.flush()
        return database, state, created, root.id, route.id, route_branch.id


def test_memory_is_route_scoped_and_explicit_import_is_required(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            _, local = state.new_object(session, state.require_branch(session, route_branch), "claim", "local", {}, "test")
            entry = memory.record(session, root_id, route_id, local.id, route_branch, "lemma", "event:one")
            assert local.id in memory.allowed_revisions(session, root_id, route_id)
            assert local.id not in memory.allowed_revisions(session, root_id, None)
            shared = memory.record(session, root_id, None, created["revision_id"], created["branch_id"], "definition", "event:two")
            assert shared.id
            assert {
                "object_id": local.object_id,
                "revision_id": local.id,
                "branch_id": route_branch,
                "section": "record",
                "offset": 0,
            } in memory.packet(session, root_id, route_id, [local.id])["read_refs"]
            assert entry.state == "current"
    finally:
        database.close()


def test_refresh_tombstones_deleted_dependency_without_touching_alternative(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            branch = state.require_branch(session, route_branch)
            _, first = state.new_object(session, branch, "claim", "first", {}, "test")
            _, alternative = state.new_object(session, branch, "claim", "alternative", {}, "test")
            old = memory.record(session, root_id, route_id, first.id, route_branch, "lemma", "event:first")
            other = memory.record(session, root_id, route_id, alternative.id, route_branch, "lemma", "event:alt")
            first.payload = {"deleted": True}
            result = memory.refresh(session, root_id)
            assert old.id in result["changed_entry_ids"]
            assert old.state == "deleted"
            assert other.state == "current"
    finally:
        database.close()


def test_purge_keeps_ledger_rows_and_marks_affected_memory_deleted(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            entry = memory.record(session, root_id, None, created["revision_id"], created["branch_id"], "definition", "event")
            result = memory.purge_project(session, created["project_id"], [created["revision_id"]])
            assert result["changed_entry_ids"] == [entry.id]
            assert session.get(SearchMemory, entry.id).state == "deleted"
            assert session.get(SearchSession, root_id) is not None
    finally:
        database.close()


def test_other_search_session_cannot_gain_route_branch_heads(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            other_run = Run(branch_id=created["branch_id"], goal_object_id=created["object_id"])
            session.add(other_run)
            session.flush()
            session.add(SearchSession(root_run_id=other_run.id, project_id=created["project_id"],
                goal_revision_id=created["revision_id"], config={"initial_read_set": {
                    created["object_id"]: created["revision_id"]}}))
            _, local = state.new_object(session, state.require_branch(session, route_branch), "claim", "other", {}, "test")
            assert local.id not in memory.allowed_revisions(session, root_id, route_id)
    finally:
        database.close()


def test_shared_import_tracks_the_source_branch_for_staleness(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            source_branch = state.require_branch(session, route_branch)
            _, dependency = state.new_object(session, source_branch, "claim", "dependency", {}, "test")
            _, local = state.new_object(session, source_branch, "claim", "source", {
                "input_revisions": {"dependency_revision_id": dependency.id},
            }, "test")
            source = memory.record(session, root_id, route_id, local.id, route_branch, "lemma", "source-event")
            target_branch = state.clone_branch(session, state.require_branch(session, created["branch_id"]), "target")
            target = SearchRoute(root_run_id=root_id, branch_id=target_branch.id,
                card_revision_id=source.revision_id, ordinal=1)
            session.add(target)
            session.flush()
            imported = memory.import_shared(session, root_id, target.id, source.id, "import-event")
            assert imported.branch_id == route_branch
            assert dependency.id in memory.allowed_revisions(session, root_id, target.id)
            _, dependent_revision = state.new_object(session, target_branch, "claim", "uses source", {
                "input_revisions": {"source_revision_id": local.id},
            }, "test")
            dependent = memory.record(session, root_id, target.id, dependent_revision.id,
                target_branch.id, "candidate", "dependent-event")
            _, independent_revision = state.new_object(session, target_branch, "claim", "independent", {}, "test")
            independent = memory.record(session, root_id, target.id, independent_revision.id,
                target_branch.id, "candidate", "independent-event")
            state.revise_object(session, {"branch_id": route_branch, "object_id": local.object_id,
                "expected_revision_id": local.id, "body": "source changed"})
            memory.refresh(session, root_id)
            assert imported.state == "stale"
            assert dependent.state == "stale"
            assert independent.state == "current"
    finally:
        database.close()


def test_erasure_scrubs_search_derivatives_and_route_card(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            route = session.get(SearchRoute, route_id)
            memory.record(session, root_id, route_id, route.card_revision_id, route_branch, "plan", "event")
            session.add(SearchWork(root_run_id=root_id, route_id=route_id, run_id=root_id,
                kind="analysis", input_snapshot={"prompt": "copied proof", "output_cap": 77}))
            session.add(SearchDecision(root_run_id=root_id, sequence=1, trigger_key="one",
                action="advance", details={"reason": "copied proof"}))
            route.progress = {"text": "copied proof"}
            preview = DeletionService(state).preview(session, {"object_id": created["object_id"]})[1]
            _, erased = DeletionService(state).erase(session, {
                "object_id": created["object_id"], "preview_token": preview["preview_token"], "confirmation": "永久删除"})
            assert erased["deleted"] is True
            assert session.get(SearchRoute, route_id).progress == {}
            assert session.get(SearchRoute, route_id).state == "closed"
            assert session.get(SearchSession, root_id).phase == "terminated"
            work = session.scalar(select(SearchWork).where(SearchWork.root_run_id == root_id))
            assert work.input_snapshot == {"output_cap": 77}
            assert session.get(Revision, route.card_revision_id).payload["deleted"] is True
    finally:
        database.close()


def test_export_freezes_search_state_and_its_revision_closure(tmp_path):
    database, state, created, root_id, route_id, route_branch = make_search(tmp_path)
    memory = MemoryService(state)
    try:
        with database.sessions.begin() as session:
            route = session.get(SearchRoute, route_id)
            memory.record(session, root_id, route_id, route.card_revision_id, route_branch, "plan", "event")
            session.get(SearchSession, root_id).config = {"request_budget": 3, "api_key": "must-not-export"}
            _, exported = ExportService(state).create(session, {
                "project_id": created["project_id"], "branch_id": created["branch_id"]})
            search = exported["bundle"]["snapshot"]["search_state"]
            assert search["sessions"][0]["session"]["root_run_id"] == root_id
            revisions = {item["id"] for item in exported["bundle"]["revisions"]}
            assert route.card_revision_id in revisions
            assert "api_key" not in exported["bundle"]
    finally:
        database.close()
