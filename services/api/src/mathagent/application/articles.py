"""Version-pinned article drafts assembled from workspace revisions."""

from mathagent.application.errors import DomainError
from mathagent.persistence.models import Head, Project, ProofPlan, Review
from sqlalchemy import select


class ArticleService:
    def __init__(self, state):
        self.state = state

    @staticmethod
    def _issue(code, message):
        return {"code": code, "message": message}

    def _issues(self, session, branch, revision_ids):
        issues = []
        snapshot = self.state.snapshot(session, branch.id)
        support = snapshot["support"]
        for revision_id in revision_ids:
            revision, obj = self.state.check_revision(session, branch, revision_id, current=True)
            reviews = list(session.scalars(select(Review).where(Review.target_revision_id == revision.id)))
            if not any(review.verdict == "passed" for review in reviews):
                issues.append(self._issue("source_unreviewed", f"来源版本 {revision.id} 没有独立审查记录。"))
            if any(review.verdict == "issues" for review in reviews):
                issues.append(self._issue("source_review_issues", f"来源版本 {revision.id} 存在审查异议，不能仅凭另一条通过记录忽略。"))
            plan = session.get(ProofPlan, revision.id)
            if plan and plan.gaps:
                issues.append(self._issue("proof_has_gaps", f"证明版本 {revision.id} 仍声明缺口。"))
            status = support["plans"].get(revision.id, support["claims"].get(revision.id, {})).get("status")
            if status and status != "supported":
                issues.append(self._issue("source_conditional", f"来源版本 {revision.id} 的支持状态为 {status}。"))
        return issues

    def create(self, session, payload):
        branch = self.state.require_branch(session, payload["branch_id"])
        title, sections = payload.get("title"), payload.get("sections")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise DomainError(422, "invalid_article_title", "文章标题不能为空或超过长度限制。")
        if not isinstance(sections, list) or not 1 <= len(sections) <= 40:
            raise DomainError(422, "invalid_article_sections", "文章必须有 1 至 40 个章节。")
        project = session.get(Project, branch.project_id)
        goal_head = session.get(Head, (branch.id, project.original_goal_id))
        if not goal_head:
            raise DomainError(422, "article_problem_missing", "当前分支缺少原题版本。")
        goal = self.state.require_revision(session, goal_head.revision_id)
        source_ids = [goal.id]
        rendered = [f"# {title.strip()}", f"\n## 原题（固定版本 {goal.id}）\n\n{goal.body}"]
        for index, section in enumerate(sections, 1):
            heading, revision_ids, transition = section.get("heading"), section.get("revision_ids"), section.get("transition", "")
            if not isinstance(heading, str) or not heading.strip() or not isinstance(transition, str):
                raise DomainError(422, "invalid_article_section", "章节标题和过渡段必须有效。")
            if not isinstance(revision_ids, list) or not revision_ids or len(revision_ids) > 100:
                raise DomainError(422, "invalid_article_section_sources", "每章必须引用 1 至 100 个版本。")
            rendered.extend((f"\n## {heading.strip()}", transition.strip()))
            for revision_id in revision_ids:
                if revision_id == goal.id:
                    rendered.append(f"\n### 来源：原题 {goal.id}（见文章开头）")
                    continue
                if revision_id in source_ids:
                    raise DomainError(422, "duplicate_article_source", "文章不能重复引用同一固定版本。")
                revision, obj = self.state.check_revision(session, branch, revision_id, current=True)
                source_ids.append(revision.id)
                rendered.append(f"\n### 来源：{obj.kind} {revision.id}\n\n{revision.body}")
        if len("\n".join(rendered)) > 200_000:
            raise DomainError(422, "article_body_too_large", "文章正文超过 200000 字符限制。")
        issues = self._issues(session, branch, source_ids)
        issues.append(self._issue("article_review_required", "文章草稿本身必须再次独立审查；节点审查不会迁移到文章。"))
        frozen_sections = [{"heading": section["heading"].strip(), "revision_ids": list(section["revision_ids"]),
                            "transition": section.get("transition", "").strip()} for section in sections]
        article_payload = {"artifact_type": "article", "title": title.strip(), "source_revision_ids": source_ids,
                           "sections": frozen_sections, "creation_issues": issues}
        obj, revision = self.state.new_object(session, branch, "artifact", "\n".join(rendered), article_payload, "human")
        self.state._add_reference(session, branch.id, revision.id)
        self.state.emit(session, branch.project_id, branch.id, "article.created", {
            "object_id": obj.id, "revision_id": revision.id, "source_revision_ids": source_ids,
        })
        return 201, {"object_id": obj.id, "revision_id": revision.id, "issues": issues}

    def check(self, session, payload):
        branch = self.state.require_branch(session, payload["branch_id"])
        article, obj = self.state.check_revision(session, branch, payload["revision_id"])
        if obj.kind != "artifact" or article.payload.get("artifact_type") != "article":
            raise DomainError(422, "not_article_revision", "指定版本不是文章草稿。")
        creation_issues = list(article.payload.get("creation_issues", article.payload.get("issues", [])))
        current_issues = []
        for source_id in article.payload.get("source_revision_ids", []):
            source = self.state.require_revision(session, source_id)
            head = session.get(Head, (branch.id, source.object_id))
            if not head or head.revision_id != source_id:
                current_issues.append(self._issue("source_version_changed", f"来源版本 {source_id} 已不是当前分支版本。"))
            else:
                current_issues.extend(self._issues(session, branch, [source_id]))
        own_reviews = list(session.scalars(select(Review).where(Review.target_revision_id == article.id)))
        whole_review = any(review.verdict == "passed" and review.coverage == "whole_plan" and review.scope.strip()
                           for review in own_reviews)
        if not whole_review:
            current_issues.append(self._issue("article_review_required", "文章草稿本身仍需整篇独立复核；局部审查与节点审查不能代替全文检查。"))
        if any(review.verdict == "issues" for review in own_reviews):
            current_issues.append(self._issue("article_review_issues", "文章自身仍有审查异议待处理。"))
        current = {(issue["code"], issue["message"]): issue for issue in current_issues}
        return 200, {"revision_id": article.id, "issues": list(current.values()),
                     "creation_issues": creation_issues, "current_issues": list(current.values()),
                     "article_review_ids": [review.id for review in own_reviews],
                     "whole_article_review_recorded": whole_review,
                     "mathematical_semantics_verified": False}
