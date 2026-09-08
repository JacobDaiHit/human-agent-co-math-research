"""Validated failure/source records with immutable version references."""

from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from mathagent.application.errors import DomainError
from mathagent.application.state import record
from mathagent.persistence.models import Head, ResearchObject, Revision, RevisionParent, now
from mathagent.persistence.research_models import BranchPresentation, ResearchRecordReference
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

Identifier = Annotated[str, Field(min_length=1, max_length=100)]
RequiredText = Annotated[str, Field(min_length=1, max_length=20_000)]


class RecordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class FailurePayload(RecordPayload):
    artifact_type: Literal["failure"] = "failure"
    target_revision_id: Identifier
    outcome: Literal["unresolved", "argument_error", "method_obstruction", "refuted"]
    assumption_revision_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    evidence_revision_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    scope: RequiredText
    retry_conditions: list[RequiredText] = Field(default_factory=list, max_length=100)
    evidence_explanation: Annotated[str, Field(max_length=20_000)] = ""

    @field_validator("assumption_revision_ids", "evidence_revision_ids", "retry_conditions")
    @classmethod
    def unique_nonblank_items(cls, values):
        cleaned = [item.strip() for item in values]
        if any(not item for item in cleaned):
            raise ValueError("Record lists must not contain blank entries.")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def explicit_evidence(self):
        if self.outcome != "unresolved" and not (
            self.evidence_revision_ids or self.evidence_explanation
        ):
            raise ValueError("A mathematical failure classification needs evidence or explanation.")
        return self


class SourcePayload(RecordPayload):
    artifact_type: Literal["source"] = "source"
    title: Annotated[str, Field(min_length=1, max_length=1000)]
    authors: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=100
    )
    url_or_identifier: Annotated[str, Field(min_length=1, max_length=4000)]
    locator: RequiredText
    accessed_on: date
    verification: Literal["provided", "unverified"] = "unverified"

    @field_validator("authors")
    @classmethod
    def nonblank_authors(cls, values):
        cleaned = [item.strip() for item in values]
        if any(not item for item in cleaned):
            raise ValueError("Author names must not be blank.")
        return list(dict.fromkeys(cleaned))

    @field_validator("url_or_identifier")
    @classmethod
    def safe_stored_location(cls, value):
        # Stored data only: no network lookup, redirects, downloads, or authentication.
        if any(ord(character) < 32 for character in value):
            raise ValueError("Source locations must not contain control characters.")
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"javascript", "data", "file", "vbscript"}:
            raise ValueError("Executable and local-file source URLs are not supported.")
        if parsed.scheme.lower() in {"http", "https"}:
            if not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Source URLs must have a host and no embedded credentials.")
        return value


def _visible_reference(session, branch, revision_id):
    revision = session.get(Revision, revision_id)
    if revision is None:
        raise DomainError(404, "revision_not_found", "引用的对象版本不存在。")
    obj = session.get(ResearchObject, revision.object_id)
    if obj.project_id != branch.project_id:
        raise DomainError(422, "cross_project_reference", "记录不能引用其他项目的版本。")
    head = session.get(Head, (branch.id, obj.id))
    if head is None:
        raise DomainError(422, "object_outside_branch", "引用对象不在当前分支。")
    # A shared object may have a newer version only on a sibling branch. Accept
    # the selected head or its ancestry, not arbitrary sibling versions.
    pending, seen = [head.revision_id], set()
    while pending:
        current = pending.pop()
        if current == revision_id:
            return revision, obj
        if current in seen:
            continue
        seen.add(current)
        pending.extend(
            session.scalars(
                select(RevisionParent.parent_id).where(RevisionParent.revision_id == current)
            )
        )
    raise DomainError(422, "revision_outside_branch", "引用版本不是当前分支所选版本或其历史。")


def validate_record_payload(kind, payload, session, branch):
    """Return canonical payload; generic draft payloads remain unrestricted.

    Call on both creation and each new revision, before any state is written.
    Callers must also prevent removal of a pre-existing managed artifact_type.
    Validation failures intentionally omit supplied payload values.
    """
    artifact_type = payload.get("artifact_type")
    if not isinstance(artifact_type, str) or artifact_type not in {"failure", "source"}:
        return payload
    if kind != "artifact":
        raise DomainError(422, "invalid_record_kind", "失败和来源记录必须保存为研究产物。")
    schema = FailurePayload if artifact_type == "failure" else SourcePayload
    try:
        normalized = schema.model_validate(payload).model_dump(mode="json")
    except (ValidationError, ValueError) as error:
        raise DomainError(
            422,
            "invalid_research_record",
            "研究记录字段不完整或无效；数学失败须提供证据版本或明确说明。",
        ) from error
    if artifact_type == "failure":
        _visible_reference(session, branch, normalized["target_revision_id"])
        for revision_id in normalized["assumption_revision_ids"]:
            _, obj = _visible_reference(session, branch, revision_id)
            if obj.kind not in {"claim", "context"}:
                raise DomainError(422, "invalid_assumption", "假设只能引用命题或上下文版本。")
        for revision_id in normalized["evidence_revision_ids"]:
            _visible_reference(session, branch, revision_id)
    return normalized


def record_reference_ids(payload):
    if payload.get("artifact_type") != "failure":
        return []
    return list(
        dict.fromkeys(
            [payload["target_revision_id"]]
            + payload.get("assumption_revision_ids", [])
            + payload.get("evidence_revision_ids", [])
        )
    )


def save_record_references(session, revision_id, payload):
    """Append FK-backed references for a newly created immutable revision."""
    if payload.get("artifact_type") != "failure":
        return
    for role, targets in (
        ("target", [payload["target_revision_id"]]),
        ("assumption", payload.get("assumption_revision_ids", [])),
        ("evidence", payload.get("evidence_revision_ids", [])),
    ):
        for target in dict.fromkeys(targets):
            session.add(
                ResearchRecordReference(
                    record_revision_id=revision_id, target_revision_id=target, role=role
                )
            )


class ResearchRecordsService:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    def _create(self, session, payload, artifact_type, author):
        # Author is an argument from the authenticated route/worker dispatcher;
        # user/model payloads cannot choose it.
        if not isinstance(author, str) or not author.strip():
            raise DomainError(422, "invalid_record_author", "记录需要可信执行身份。")
        branch = self.state.require_branch(session, payload["branch_id"])
        body = payload["body"]
        if not isinstance(body, str) or not body.strip() or len(body) > 200_000:
            raise DomainError(422, "invalid_record_body", "记录正文不能为空或超过长度限制。")
        fields = {key: value for key, value in payload.items() if key not in {"branch_id", "body"}}
        fields["artifact_type"] = artifact_type
        fields = validate_record_payload("artifact", fields, session, branch)
        obj, revision = self.state.new_object(session, branch, "artifact", body, fields, author)
        self.state._add_reference(session, branch.id, revision.id)
        result = {"object_id": obj.id, "revision_id": revision.id, "branch_id": branch.id}
        self.state.emit(
            session, branch.project_id, branch.id, f"{artifact_type}.recorded", result, author
        )
        return 201, result

    def create_failure(self, session, payload, *, author="human"):
        return self._create(session, payload, "failure", author)

    def create_source(self, session, payload, *, author="human"):
        return self._create(session, payload, "source", author)

    @staticmethod
    def presentation(session, branch_id):
        presentation = session.get(BranchPresentation, branch_id)
        return (
            record(presentation)
            if presentation
            else {
                "branch_id": branch_id,
                "version": 0,
                "hidden_object_ids": [],
                "collapsed_object_ids": [],
                "archived": False,
                "updated_at": None,
            }
        )

    def get_presentation(self, branch_id):
        with self.db.sessions() as session:
            self.state.require_branch(session, branch_id)
            return self.presentation(session, branch_id)

    def update_presentation(self, session, payload):
        branch = self.state.require_branch(session, payload["branch_id"])
        current = self.presentation(session, branch.id)
        if current["version"] != payload["expected_version"]:
            raise DomainError(
                409,
                "presentation_conflict",
                "分支显示设置已变化，请重新加载。",
                current_version=current["version"],
                presentation=current,
            )
        for object_id in payload["hidden_object_ids"] + payload["collapsed_object_ids"]:
            if not session.get(Head, (branch.id, object_id)):
                raise DomainError(422, "object_outside_branch", "显示设置只能包含当前分支对象。")
        presentation = session.get(BranchPresentation, branch.id)
        if presentation is None:
            presentation = BranchPresentation(branch_id=branch.id)
            session.add(presentation)
        presentation.hidden_object_ids = sorted(set(payload["hidden_object_ids"]))
        presentation.collapsed_object_ids = sorted(set(payload["collapsed_object_ids"]))
        presentation.archived = payload["archived"]
        presentation.version = current["version"] + 1
        presentation.updated_at = now()
        session.flush()
        result = record(presentation)
        self.state.emit(
            session, branch.project_id, branch.id, "workspace.presentation_updated", result
        )
        return 200, result
