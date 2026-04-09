from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    name: str
    number: int


@dataclass(frozen=True)
class PullRequestMeta:
    ref: PullRequestRef
    title: str
    url: str
    author: str
    state: str
    is_draft: bool
    body: str
    updated_at: str
    changed_files: int | None = None
    labels: tuple[str, ...] = ()
    kind: str = "pr"
    reactions_summary: str | None = None
    can_edit_body: bool = False
    is_merged: bool = False
    head_ref_name: str | None = None
    head_ref_repo: str | None = None
    head_ref_oid: str | None = None
    head_ref_deleted: bool | None = None
    node_id: str | None = None
    merge_state_status: str | None = None
    mergeable: str | None = None
    review_decision: str | None = None
    requires_approving_reviews: bool | None = None
    required_approving_review_count: int | None = None
    requires_code_owner_reviews: bool | None = None
    approved_review_count: int | None = None
    requires_status_checks: bool | None = None
    base_ref_name: str | None = None
    base_ref_oid: str | None = None
    merge_commit_allowed: bool | None = None
    squash_merge_allowed: bool | None = None
    rebase_merge_allowed: bool | None = None
    co_author_trailers: tuple[str, ...] = ()
    conflict_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimelineWindow:
    after: datetime | None = None
    before: datetime | None = None
    after_text: str | None = None
    before_text: str | None = None

    @property
    def active(self) -> bool:
        return self.after is not None or self.before is not None


@dataclass(frozen=True)
class PageInfo:
    has_next_page: bool
    has_previous_page: bool
    start_cursor: str | None
    end_cursor: str | None


@dataclass(frozen=True)
class TimelineEvent:
    timestamp: datetime
    kind: str
    actor: str
    summary: str
    source_id: str
    full_text: str | None = None
    related_timestamps: tuple[datetime, ...] = ()
    is_truncated: bool = False
    resolved_hidden_count: int = 0
    minimized_hidden_count: int = 0
    minimized_hidden_reasons: str | None = None
    editable_comment_id: str | None = None
    reactions_summary: str | None = None
    details_collapsed_count: int = 0


@dataclass(frozen=True)
class TimelinePage:
    items: list[TimelineEvent]
    total_count: int
    page_info: PageInfo
    absolute_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class CheckItem:
    name: str
    kind: str
    status: str
    passed: bool
    details_url: str | None = None
    run_id: int | None = None
    job_id: int | None = None


@dataclass(frozen=True)
class RepoDocument:
    path: str


@dataclass(frozen=True)
class RepoBranchProtection:
    pattern: str
    source: str = "rest"
    requires_status_checks: bool = False
    required_status_check_contexts: tuple[str, ...] = ()
    requires_approving_reviews: bool | None = None
    required_approving_review_count: int | None = None
    requires_code_owner_reviews: bool | None = None
    is_admin_enforced: bool | None = None


@dataclass(frozen=True)
class RepoPreflight:
    owner: str
    name: str
    url: str
    default_branch: str
    ssh_url: str | None = None
    description: str | None = None
    homepage_url: str | None = None
    viewer_permission: str | None = None
    can_push: bool = False
    fork_recommended: bool = False
    is_fork: bool = False
    parent_repo: str | None = None
    tree_truncated: bool = False
    contributing_docs: tuple[RepoDocument, ...] = ()
    agents_docs: tuple[RepoDocument, ...] = ()
    pr_templates: tuple[RepoDocument, ...] = ()
    codeowners_files: tuple[RepoDocument, ...] = ()
    branch_protection: RepoBranchProtection | None = None


@dataclass(frozen=True)
class PullRequestDiffFile:
    path: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None
    previous_path: str | None = None


@dataclass(frozen=True)
class PullRequestDiffPage:
    page: int
    page_size: int
    total_files: int
    total_pages: int
    files: tuple[PullRequestDiffFile, ...]


@dataclass(frozen=True)
class ReviewCommentSummary:
    comment_id: str
    author: str
    body_preview: str
    is_outdated: bool = False
    is_minimized: bool = False
    minimized_reason: str | None = None


@dataclass(frozen=True)
class ReviewThreadSummary:
    thread_id: str
    path: str
    is_resolved: bool
    comment_count: int
    is_outdated: bool = False
    anchor_side: str | None = None
    anchor_line: int | None = None
    right_lines: tuple[int, ...] = ()
    left_lines: tuple[int, ...] = ()
    display_ref: str | None = None
    comments: tuple[ReviewCommentSummary, ...] = ()


@dataclass
class TimelineContext:
    owner: str
    name: str
    number: int
    page_size: int
    total_count: int
    total_pages: int
    title: str
    url: str
    author: str
    state: str
    is_draft: bool
    body: str
    updated_at: str
    fetched_at: str = ""
    timeline_loaded: bool = True
    timeline_after: str | None = None
    timeline_before: str | None = None
    timeline_unfiltered_count: int | None = None
    timeline_filtered: bool = False
    labels: tuple[str, ...] = ()
    kind: str = "pr"
    pr_reactions_summary: str | None = None
    can_edit_pr_body: bool = False
    is_merged: bool = False
    head_ref_name: str | None = None
    head_ref_repo: str | None = None
    head_ref_oid: str | None = None
    head_ref_deleted: bool | None = None
    pr_node_id: str | None = None
    merge_state_status: str | None = None
    mergeable: str | None = None
    review_decision: str | None = None
    requires_approving_reviews: bool | None = None
    required_approving_review_count: int | None = None
    requires_code_owner_reviews: bool | None = None
    approved_review_count: int | None = None
    requires_status_checks: bool | None = None
    base_ref_name: str | None = None
    base_ref_oid: str | None = None
    merge_commit_allowed: bool | None = None
    squash_merge_allowed: bool | None = None
    rebase_merge_allowed: bool | None = None
    co_author_trailers: tuple[str, ...] = ()
    conflict_files: tuple[str, ...] = ()
    forward_after_by_page: dict[int, str | None] = field(default_factory=lambda: cast("dict[int, str | None]", {}))
    backward_before_by_page: dict[int, str | None] = field(default_factory=lambda: cast("dict[int, str | None]", {}))
    filtered_pages: dict[int, TimelinePage] = field(default_factory=lambda: cast("dict[int, TimelinePage]", {}))

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "name": self.name,
            "number": self.number,
            "page_size": self.page_size,
            "total_count": self.total_count,
            "total_pages": self.total_pages,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "state": self.state,
            "is_draft": self.is_draft,
            "body": self.body,
            "updated_at": self.updated_at,
            "fetched_at": self.fetched_at,
            "timeline_loaded": self.timeline_loaded,
            "timeline_after": self.timeline_after,
            "timeline_before": self.timeline_before,
            "timeline_unfiltered_count": self.timeline_unfiltered_count,
            "timeline_filtered": self.timeline_filtered,
            "labels": list(self.labels),
            "kind": self.kind,
            "pr_reactions_summary": self.pr_reactions_summary,
            "can_edit_pr_body": self.can_edit_pr_body,
            "is_merged": self.is_merged,
            "head_ref_name": self.head_ref_name,
            "head_ref_repo": self.head_ref_repo,
            "head_ref_oid": self.head_ref_oid,
            "head_ref_deleted": self.head_ref_deleted,
            "pr_node_id": self.pr_node_id,
            "merge_state_status": self.merge_state_status,
            "mergeable": self.mergeable,
            "review_decision": self.review_decision,
            "requires_approving_reviews": self.requires_approving_reviews,
            "required_approving_review_count": self.required_approving_review_count,
            "requires_code_owner_reviews": self.requires_code_owner_reviews,
            "approved_review_count": self.approved_review_count,
            "requires_status_checks": self.requires_status_checks,
            "base_ref_name": self.base_ref_name,
            "base_ref_oid": self.base_ref_oid,
            "merge_commit_allowed": self.merge_commit_allowed,
            "squash_merge_allowed": self.squash_merge_allowed,
            "rebase_merge_allowed": self.rebase_merge_allowed,
            "co_author_trailers": list(self.co_author_trailers),
            "conflict_files": list(self.conflict_files),
            "forward_after_by_page": {str(k): v for k, v in self.forward_after_by_page.items()},
            "backward_before_by_page": {str(k): v for k, v in self.backward_before_by_page.items()},
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TimelineContext:
        return cls(
            owner=_as_str(value.get("owner"), "unknown"),
            name=_as_str(value.get("name"), "unknown"),
            number=_as_int(value.get("number"), 0),
            page_size=_as_int(value.get("page_size"), 8),
            total_count=_as_int(value.get("total_count"), 0),
            total_pages=_as_int(value.get("total_pages"), 1),
            title=_as_str(value.get("title"), ""),
            url=_as_str(value.get("url"), ""),
            author=_as_str(value.get("author"), "unknown"),
            state=_as_str(value.get("state"), "UNKNOWN"),
            is_draft=bool(value.get("is_draft")),
            body=_as_str(value.get("body"), ""),
            updated_at=_as_str(value.get("updated_at"), ""),
            fetched_at=_as_str(value.get("fetched_at"), ""),
            timeline_loaded=(
                _as_int(value.get("total_pages"), 0) > 0
                if value.get("timeline_loaded") is None
                else bool(value.get("timeline_loaded"))
            ),
            timeline_after=_as_str_optional(value.get("timeline_after")),
            timeline_before=_as_str_optional(value.get("timeline_before")),
            timeline_unfiltered_count=(
                None
                if value.get("timeline_unfiltered_count") is None
                else _as_int(value.get("timeline_unfiltered_count"), 0)
            ),
            timeline_filtered=bool(value.get("timeline_filtered")),
            labels=tuple(_as_str(item, "") for item in _as_list(value.get("labels")) if item),
            kind=_as_str(value.get("kind"), "pr"),
            pr_reactions_summary=_as_str_optional(value.get("pr_reactions_summary")),
            can_edit_pr_body=bool(value.get("can_edit_pr_body")),
            is_merged=bool(value.get("is_merged")),
            head_ref_name=_as_str_optional(value.get("head_ref_name")),
            head_ref_repo=_as_str_optional(value.get("head_ref_repo")),
            head_ref_oid=_as_str_optional(value.get("head_ref_oid")),
            head_ref_deleted=(None if value.get("head_ref_deleted") is None else bool(value.get("head_ref_deleted"))),
            pr_node_id=_as_str_optional(value.get("pr_node_id")),
            merge_state_status=_as_str_optional(value.get("merge_state_status")),
            mergeable=_as_str_optional(value.get("mergeable")),
            review_decision=_as_str_optional(value.get("review_decision")),
            requires_approving_reviews=(
                None
                if value.get("requires_approving_reviews") is None
                else bool(value.get("requires_approving_reviews"))
            ),
            required_approving_review_count=(
                None
                if value.get("required_approving_review_count") is None
                else _as_int(value.get("required_approving_review_count"), 0)
            ),
            requires_code_owner_reviews=(
                None
                if value.get("requires_code_owner_reviews") is None
                else bool(value.get("requires_code_owner_reviews"))
            ),
            approved_review_count=(
                None if value.get("approved_review_count") is None else _as_int(value.get("approved_review_count"), 0)
            ),
            requires_status_checks=(
                None if value.get("requires_status_checks") is None else bool(value.get("requires_status_checks"))
            ),
            base_ref_name=_as_str_optional(value.get("base_ref_name")),
            base_ref_oid=_as_str_optional(value.get("base_ref_oid")),
            merge_commit_allowed=(
                None if value.get("merge_commit_allowed") is None else bool(value.get("merge_commit_allowed"))
            ),
            squash_merge_allowed=(
                None if value.get("squash_merge_allowed") is None else bool(value.get("squash_merge_allowed"))
            ),
            rebase_merge_allowed=(
                None if value.get("rebase_merge_allowed") is None else bool(value.get("rebase_merge_allowed"))
            ),
            co_author_trailers=tuple(_as_str(item, "") for item in _as_list(value.get("co_author_trailers")) if item),
            conflict_files=tuple(_as_str(item, "") for item in _as_list(value.get("conflict_files")) if item),
            forward_after_by_page={
                int(k): None if v is None else str(v)
                for k, v in _ensure_dict(value.get("forward_after_by_page")).items()
            },
            backward_before_by_page={
                int(k): None if v is None else str(v)
                for k, v in _ensure_dict(value.get("backward_before_by_page")).items()
            },
        )


def _ensure_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        raw = cast("dict[object, object]", value)
        return {str(k): v for k, v in raw.items()}
    return {}


def _as_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_str(value: object, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_str_optional(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast("list[object]", value)
    return []
