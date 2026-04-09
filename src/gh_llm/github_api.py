from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, urlparse

from gh_llm.diagnostics import GhCommandError
from gh_llm.invocation import display_command, display_command_with
from gh_llm.models import (
    CheckItem,
    PageInfo,
    PullRequestDiffFile,
    PullRequestDiffPage,
    PullRequestMeta,
    PullRequestRef,
    RepoBranchProtection,
    RepoDocument,
    RepoPreflight,
    ReviewCommentSummary,
    ReviewThreadSummary,
    TimelineEvent,
    TimelinePage,
)

if TYPE_CHECKING:
    from gh_llm.models import TimelineWindow

MAX_INLINE_TEXT = 8000
MAX_INLINE_LINES = 200
DEFAULT_REVIEW_DIFF_HUNK_LINES = 12
GRAPHQL_MAX_ATTEMPTS = 4
GRAPHQL_BACKOFF_BASE_SECONDS = 0.25
GRAPHQL_BACKOFF_MAX_SECONDS = 2.0
DETAILS_BLOCK_RE = re.compile(r"(?is)<details\b[^>]*>(.*?)</details>")
SUMMARY_RE = re.compile(r"(?is)<summary\b[^>]*>(.*?)</summary>")
HTML_TAG_RE = re.compile(r"(?is)<[^>]+>")
CO_AUTHORED_BY_RE = re.compile(r"(?im)^\s*Co-authored-by:\s*([^\n<>]+?)\s*<([^<>\n]+)>\s*$")
GIT_CONFLICT_FILE_RE = re.compile(r"^CONFLICT \([^)]*\): Merge conflict in (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ReferenceSubject:
    type: str
    number: int
    repo: str
    detail: str


FORWARD_TIMELINE_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$pageSize:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      timelineItems(first:$pageSize,after:$after,itemTypes:[ISSUE_COMMENT,PULL_REQUEST_REVIEW,PULL_REQUEST_COMMIT,REVIEW_DISMISSED_EVENT,CROSS_REFERENCED_EVENT,REFERENCED_EVENT,LABELED_EVENT,UNLABELED_EVENT,RENAMED_TITLE_EVENT,HEAD_REF_FORCE_PUSHED_EVENT,MERGED_EVENT,CLOSED_EVENT,REOPENED_EVENT]){
        totalCount
        pageInfo{hasNextPage hasPreviousPage startCursor endCursor}
        nodes{
          __typename
          ... on IssueComment{
            id
            url
            createdAt
            body
            isMinimized
            minimizedReason
            author{login ... on User{name}}
            reactionGroups{content users{totalCount}}
          }
          ... on PullRequestReview{
            id
            submittedAt
            state
            body
            isMinimized
            minimizedReason
            author{login ... on User{name}}
          }
          ... on ReviewDismissedEvent{
            id
            createdAt
            dismissalMessage
            actor{login ... on User{name}}
            review{
              author{login ... on User{name}}
              submittedAt
            }
          }
          ... on PullRequestCommit{ commit{ oid committedDate messageHeadline message authors(first:1){nodes{name user{login}}} } }
          ... on CrossReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            source{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on ReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            subject{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on LabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on UnlabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on RenamedTitleEvent{ id createdAt actor{login ... on User{name}} previousTitle currentTitle }
          ... on HeadRefForcePushedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            ref{name}
            beforeCommit{oid}
            afterCommit{oid}
          }
          ... on MergedEvent{ id createdAt actor{login ... on User{name}} }
          ... on ClosedEvent{ id createdAt actor{login ... on User{name}} }
          ... on ReopenedEvent{ id createdAt actor{login ... on User{name}} }
        }
      }
    }
  }
}
""".strip()

ISSUE_FORWARD_TIMELINE_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$pageSize:Int!,$after:String){
  repository(owner:$owner,name:$name){
    issue(number:$number){
      timelineItems(first:$pageSize,after:$after,itemTypes:[ISSUE_COMMENT,CROSS_REFERENCED_EVENT,REFERENCED_EVENT,LABELED_EVENT,UNLABELED_EVENT,RENAMED_TITLE_EVENT,MARKED_AS_DUPLICATE_EVENT,CLOSED_EVENT,REOPENED_EVENT]){
        totalCount
        pageInfo{hasNextPage hasPreviousPage startCursor endCursor}
        nodes{
          __typename
          ... on IssueComment{
            id
            url
            createdAt
            body
            isMinimized
            minimizedReason
            author{login ... on User{name}}
            reactionGroups{content users{totalCount}}
          }
          ... on CrossReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            source{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on ReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            subject{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on LabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on UnlabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on RenamedTitleEvent{ id createdAt actor{login ... on User{name}} previousTitle currentTitle }
          ... on MarkedAsDuplicateEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            canonical{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
            duplicate{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on ClosedEvent{ id createdAt actor{login ... on User{name}} }
          ... on ReopenedEvent{ id createdAt actor{login ... on User{name}} }
        }
      }
    }
  }
}
""".strip()

BACKWARD_TIMELINE_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$pageSize:Int!,$before:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      timelineItems(last:$pageSize,before:$before,itemTypes:[ISSUE_COMMENT,PULL_REQUEST_REVIEW,PULL_REQUEST_COMMIT,REVIEW_DISMISSED_EVENT,CROSS_REFERENCED_EVENT,REFERENCED_EVENT,LABELED_EVENT,UNLABELED_EVENT,RENAMED_TITLE_EVENT,HEAD_REF_FORCE_PUSHED_EVENT,MERGED_EVENT,CLOSED_EVENT,REOPENED_EVENT]){
        totalCount
        pageInfo{hasNextPage hasPreviousPage startCursor endCursor}
        nodes{
          __typename
          ... on IssueComment{
            id
            url
            createdAt
            body
            isMinimized
            minimizedReason
            author{login ... on User{name}}
            reactionGroups{content users{totalCount}}
          }
          ... on PullRequestReview{
            id
            submittedAt
            state
            body
            isMinimized
            minimizedReason
            author{login ... on User{name}}
          }
          ... on ReviewDismissedEvent{
            id
            createdAt
            dismissalMessage
            actor{login ... on User{name}}
            review{
              author{login ... on User{name}}
              submittedAt
            }
          }
          ... on PullRequestCommit{ commit{ oid committedDate messageHeadline message authors(first:1){nodes{name user{login}}} } }
          ... on CrossReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            source{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on ReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            subject{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on LabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on UnlabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on RenamedTitleEvent{ id createdAt actor{login ... on User{name}} previousTitle currentTitle }
          ... on HeadRefForcePushedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            ref{name}
            beforeCommit{oid}
            afterCommit{oid}
          }
          ... on MergedEvent{ id createdAt actor{login ... on User{name}} }
          ... on ClosedEvent{ id createdAt actor{login ... on User{name}} }
          ... on ReopenedEvent{ id createdAt actor{login ... on User{name}} }
        }
      }
    }
  }
}
""".strip()

ISSUE_BACKWARD_TIMELINE_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$pageSize:Int!,$before:String){
  repository(owner:$owner,name:$name){
    issue(number:$number){
      timelineItems(last:$pageSize,before:$before,itemTypes:[ISSUE_COMMENT,CROSS_REFERENCED_EVENT,REFERENCED_EVENT,LABELED_EVENT,UNLABELED_EVENT,RENAMED_TITLE_EVENT,MARKED_AS_DUPLICATE_EVENT,CLOSED_EVENT,REOPENED_EVENT]){
        totalCount
        pageInfo{hasNextPage hasPreviousPage startCursor endCursor}
        nodes{
          __typename
          ... on IssueComment{
            id
            url
            createdAt
            body
            isMinimized
            minimizedReason
            author{login ... on User{name}}
            reactionGroups{content users{totalCount}}
          }
          ... on CrossReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            source{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on ReferencedEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            subject{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on LabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on UnlabeledEvent{ id createdAt actor{login ... on User{name}} label{name} }
          ... on RenamedTitleEvent{ id createdAt actor{login ... on User{name}} previousTitle currentTitle }
          ... on MarkedAsDuplicateEvent{
            id
            createdAt
            actor{login ... on User{name}}
            isCrossRepository
            canonical{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
            duplicate{
              __typename
              ... on PullRequest{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
              ... on Issue{
                number
                title
                author{login ... on User{name}}
                repository{nameWithOwner}
              }
            }
          }
          ... on ClosedEvent{ id createdAt actor{login ... on User{name}} }
          ... on ReopenedEvent{ id createdAt actor{login ... on User{name}} }
        }
      }
    }
  }
}
""".strip()

REVIEW_THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{
          id
          isResolved
          comments(first:100){
            nodes{
              id
              path
              body
              line
              originalLine
              startLine
              originalStartLine
              diffHunk
              createdAt
              outdated
              isMinimized
              minimizedReason
              author{login ... on User{name}}
              reactionGroups{content users{totalCount}}
              pullRequestReview{id}
            }
          }
        }
      }
    }
  }
}
""".strip()

CHECKS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      commits(last:1){
        nodes{
          commit{
            statusCheckRollup{
              contexts(first:100){
                nodes{
                  __typename
                  ... on CheckRun{
                    name
                    status
                    conclusion
                    detailsUrl
                    databaseId
                  }
                  ... on StatusContext{
                    context
                    state
                    targetUrl
                    description
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()

PR_NODE_ID_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      id
    }
  }
}
""".strip()

ADD_PULL_REQUEST_REVIEW_QUERY = """
mutation($pullRequestId:ID!,$event:PullRequestReviewEvent!,$body:String){
  addPullRequestReview(input:{pullRequestId:$pullRequestId,event:$event,body:$body}){
    pullRequestReview{
      id
      state
    }
  }
}
""".strip()

PENDING_REVIEWS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviews(last:50){
        nodes{
          id
          state
          author{login}
        }
      }
    }
  }
}
""".strip()

SUBMIT_PULL_REQUEST_REVIEW_QUERY = """
mutation($id:ID!,$event:PullRequestReviewEvent!,$body:String){
  submitPullRequestReview(input:{pullRequestReviewId:$id,event:$event,body:$body}){
    pullRequestReview{
      id
      state
    }
  }
}
""".strip()

ADD_PULL_REQUEST_REVIEW_THREAD_QUERY = """
mutation(
  $pullRequestId:ID!
  $path:String!
  $line:Int!
  $side:DiffSide!
  $body:String!
  $startLine:Int
  $startSide:DiffSide
){
  addPullRequestReviewThread(
    input:{
      pullRequestId:$pullRequestId
      path:$path
      line:$line
      side:$side
      body:$body
      startLine:$startLine
      startSide:$startSide
    }
  ){
    thread{
      id
      comments(first:1){
        nodes{id}
      }
    }
  }
}
""".strip()

COMMENT_NODE_QUERY = """
query($id:ID!){
  node(id:$id){
    __typename
    ... on IssueComment{
      id
      createdAt
      body
      isMinimized
      minimizedReason
      author{login ... on User{name}}
      reactionGroups{content users{totalCount}}
    }
    ... on PullRequestReviewComment{
      id
      createdAt
      body
      outdated
      isMinimized
      minimizedReason
      path
      line
      originalLine
      diffHunk
      author{login ... on User{name}}
      reactionGroups{content users{totalCount}}
      pullRequestReview{id}
    }
  }
}
""".strip()

PULL_REQUEST_ACTIONS_META_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    mergeCommitAllowed
    squashMergeAllowed
    rebaseMergeAllowed
    pullRequest(number:$number){
      id
      merged
      reviewDecision
      baseRefName
      baseRefOid
      headRefName
      headRefOid
      headRepository{nameWithOwner}
      reviews(last:100){
        nodes{
          state
          author{login}
        }
      }
      baseRef{
        branchProtectionRule{
          requiresApprovingReviews
          requiredApprovingReviewCount
          requiresCodeOwnerReviews
          requiresStatusChecks
        }
      }
    }
  }
}
""".strip()

REPOSITORY_REF_EXISTS_QUERY = """
query($owner:String!,$name:String!,$qualifiedName:String!){
  repository(owner:$owner,name:$name){
    ref(qualifiedName:$qualifiedName){
      id
    }
  }
}
""".strip()


class GitHubClient:
    def __init__(self) -> None:
        self._file_lines_cache: dict[tuple[str, str, str, str], tuple[str, ...] | None] = {}
        self._review_threads_raw_cache: dict[tuple[str, str, int], tuple[dict[str, object], ...]] = {}
        self._review_threads_cache: dict[tuple[str, str, int], dict[str, list[dict[str, object]]]] = {}
        self._viewer_login: str | None = None

    def resolve_pull_request(self, selector: str | None, repo: str | None) -> PullRequestMeta:
        fields = [
            "number",
            "title",
            "url",
            "author",
            "state",
            "isDraft",
            "body",
            "updatedAt",
            "changedFiles",
            "labels",
            "reactionGroups",
            "mergeStateStatus",
            "mergeable",
            "commits",
        ]
        cmd = ["gh", "pr", "view"]
        if selector:
            cmd.append(selector)
        if repo:
            cmd.extend(["--repo", repo])
        cmd.extend(["--json", ",".join(fields)])

        payload = _run_command_json(
            cmd,
            max_attempts=GRAPHQL_MAX_ATTEMPTS,
            backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
            backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
        )
        number = _as_int(payload.get("number"), context="number")
        title = _as_optional_str(payload.get("title")) or ""
        url = _as_optional_str(payload.get("url")) or ""
        author = _get_login(payload.get("author"))
        state = _as_optional_str(payload.get("state")) or "UNKNOWN"
        is_draft = bool(payload.get("isDraft"))
        body = _as_optional_str(payload.get("body")) or ""
        updated_at = _as_optional_str(payload.get("updatedAt")) or ""
        changed_files = _as_optional_int(payload.get("changedFiles"))
        labels = tuple(_extract_label_names(payload))
        reactions_summary = _format_reactions(payload.get("reactionGroups"))

        owner, name = _parse_owner_repo(url)
        ref = PullRequestRef(owner=owner, name=name, number=number)
        if self._viewer_login is None:
            self._viewer_login = self._get_viewer_login()
        can_edit_body = author == (self._viewer_login or "")
        pr_meta = self._fetch_pull_request_actions_meta(ref)
        pr_node_id = _as_optional_str(pr_meta.get("id"))
        is_merged = bool(pr_meta.get("merged"))
        head_ref_name = _as_optional_str(pr_meta.get("headRefName"))
        base_ref_name = _as_optional_str(pr_meta.get("baseRefName"))
        base_ref_oid = _as_optional_str(pr_meta.get("baseRefOid"))
        head_repo_obj = _as_dict_optional(pr_meta.get("headRepository"))
        head_repo = _as_optional_str(head_repo_obj.get("nameWithOwner")) if head_repo_obj else None
        head_ref_oid = _as_optional_str(pr_meta.get("headRefOid"))
        head_ref_deleted: bool | None = None
        if state in {"CLOSED", "MERGED"}:
            head_ref_deleted = self._is_head_ref_deleted(head_repo=head_repo, head_ref_name=head_ref_name)
        merge_state_status = _as_optional_str(payload.get("mergeStateStatus"))
        mergeable = _as_optional_str(payload.get("mergeable"))
        review_decision = _as_optional_str(pr_meta.get("reviewDecision"))
        base_ref = _as_dict_optional(pr_meta.get("baseRef"))
        bp_obj = _as_dict_optional(base_ref.get("branchProtectionRule")) if base_ref is not None else None
        bp = _as_dict_optional(bp_obj)
        requires_approving_reviews = None if bp is None else bool(bp.get("requiresApprovingReviews"))
        required_approving_review_count = None
        if bp is not None:
            raw_required_count = bp.get("requiredApprovingReviewCount")
            if isinstance(raw_required_count, int):
                required_approving_review_count = raw_required_count
        requires_code_owner_reviews = None if bp is None else bool(bp.get("requiresCodeOwnerReviews"))
        requires_status_checks = None if bp is None else bool(bp.get("requiresStatusChecks"))
        approved_review_count = _count_approved_reviewers(_as_dict_optional(pr_meta.get("reviews")))
        merge_commit_allowed = _as_optional_bool(pr_meta.get("mergeCommitAllowed"))
        squash_merge_allowed = _as_optional_bool(pr_meta.get("squashMergeAllowed"))
        rebase_merge_allowed = _as_optional_bool(pr_meta.get("rebaseMergeAllowed"))
        co_author_trailers = tuple(_extract_co_author_trailers(payload))
        return PullRequestMeta(
            ref=ref,
            title=title,
            url=url,
            author=author,
            state=state,
            is_draft=is_draft,
            body=body,
            updated_at=updated_at,
            changed_files=changed_files,
            labels=labels,
            kind="pr",
            reactions_summary=reactions_summary,
            can_edit_body=can_edit_body,
            is_merged=is_merged,
            head_ref_name=head_ref_name,
            head_ref_repo=head_repo,
            head_ref_oid=head_ref_oid,
            head_ref_deleted=head_ref_deleted,
            node_id=pr_node_id,
            merge_state_status=merge_state_status,
            mergeable=mergeable,
            review_decision=review_decision,
            requires_approving_reviews=requires_approving_reviews,
            required_approving_review_count=required_approving_review_count,
            requires_code_owner_reviews=requires_code_owner_reviews,
            approved_review_count=approved_review_count,
            requires_status_checks=requires_status_checks,
            base_ref_name=base_ref_name,
            base_ref_oid=base_ref_oid,
            merge_commit_allowed=merge_commit_allowed,
            squash_merge_allowed=squash_merge_allowed,
            rebase_merge_allowed=rebase_merge_allowed,
            co_author_trailers=co_author_trailers,
            conflict_files=(),
        )

    def resolve_issue(self, selector: str | None, repo: str | None) -> PullRequestMeta:
        fields = [
            "number",
            "title",
            "url",
            "author",
            "state",
            "body",
            "updatedAt",
            "labels",
            "reactionGroups",
        ]
        cmd = ["gh", "issue", "view"]
        if selector:
            cmd.append(selector)
        if repo:
            cmd.extend(["--repo", repo])
        cmd.extend(["--json", ",".join(fields)])

        payload = _run_command_json(
            cmd,
            max_attempts=GRAPHQL_MAX_ATTEMPTS,
            backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
            backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
        )
        number = _as_int(payload.get("number"), context="number")
        title = _as_optional_str(payload.get("title")) or ""
        url = _as_optional_str(payload.get("url")) or ""
        author = _get_login(payload.get("author"))
        state = _as_optional_str(payload.get("state")) or "UNKNOWN"
        body = _as_optional_str(payload.get("body")) or ""
        updated_at = _as_optional_str(payload.get("updatedAt")) or ""
        labels = tuple(_extract_label_names(payload))
        reactions_summary = _format_reactions(payload.get("reactionGroups"))

        owner, name = _parse_owner_repo(url)
        ref = PullRequestRef(owner=owner, name=name, number=number)
        if self._viewer_login is None:
            self._viewer_login = self._get_viewer_login()
        can_edit_body = author == (self._viewer_login or "")
        return PullRequestMeta(
            ref=ref,
            title=title,
            url=url,
            author=author,
            state=state,
            is_draft=False,
            body=body,
            updated_at=updated_at,
            labels=labels,
            kind="issue",
            reactions_summary=reactions_summary,
            can_edit_body=can_edit_body,
        )

    def resolve_repo_preflight(self, repo: str) -> RepoPreflight:
        fields = [
            "nameWithOwner",
            "description",
            "homepageUrl",
            "isFork",
            "parent",
            "url",
            "sshUrl",
            "viewerPermission",
            "defaultBranchRef",
        ]
        payload = _run_command_json(["gh", "repo", "view", repo, "--json", ",".join(fields)])

        name_with_owner = (_as_optional_str(payload.get("nameWithOwner")) or repo).strip()
        if "/" not in name_with_owner:
            raise RuntimeError(f"invalid repository identifier: {name_with_owner}")
        owner, name = name_with_owner.split("/", 1)

        default_branch_obj = _as_dict_optional(payload.get("defaultBranchRef"))
        default_branch = _as_optional_str(default_branch_obj.get("name")) if default_branch_obj is not None else None
        if not default_branch:
            raise RuntimeError("failed to resolve default branch")

        encoded_branch = quote(default_branch, safe="")
        tree_payload = _run_command_json(["gh", "api", f"repos/{owner}/{name}/git/trees/{encoded_branch}?recursive=1"])
        tree_truncated = bool(tree_payload.get("truncated"))
        tree_items = _as_list(tree_payload.get("tree"))
        if tree_truncated:
            tree_items = _merge_repo_tree_items(
                tree_items,
                self._collect_common_onboarding_tree_items(
                    owner=owner,
                    name=name,
                    default_branch=default_branch,
                ),
            )

        parent_obj = _as_dict_optional(payload.get("parent"))
        viewer_permission = _normalized_optional_str(payload.get("viewerPermission"))
        can_push = viewer_permission in {"ADMIN", "MAINTAIN", "WRITE"}
        is_fork = bool(payload.get("isFork"))
        parent_repo = _extract_parent_repo_full_name(parent_obj)
        branch_protection = self._resolve_default_branch_protection(
            owner=owner,
            name=name,
            default_branch=default_branch,
        )

        return RepoPreflight(
            owner=owner,
            name=name,
            url=_normalized_optional_str(payload.get("url")) or f"https://github.com/{owner}/{name}",
            default_branch=default_branch,
            ssh_url=_normalized_optional_str(payload.get("sshUrl")),
            description=_normalized_optional_str(payload.get("description")),
            homepage_url=_normalized_optional_str(payload.get("homepageUrl")),
            viewer_permission=viewer_permission,
            can_push=can_push,
            fork_recommended=(not can_push),
            is_fork=is_fork,
            parent_repo=parent_repo,
            tree_truncated=tree_truncated,
            contributing_docs=_collect_repo_documents(tree_items, kind="contributing"),
            agents_docs=_collect_repo_documents(tree_items, kind="agents"),
            pr_templates=_collect_repo_documents(tree_items, kind="pr_template"),
            codeowners_files=_collect_repo_documents(tree_items, kind="codeowners"),
            branch_protection=branch_protection,
        )

    def _collect_common_onboarding_tree_items(
        self,
        *,
        owner: str,
        name: str,
        default_branch: str,
    ) -> list[object]:
        items: list[object] = []
        for directory in ("", ".github", ".github/PULL_REQUEST_TEMPLATE"):
            items.extend(
                self._fetch_repo_directory_entries(
                    owner=owner,
                    name=name,
                    directory=directory,
                    default_branch=default_branch,
                )
            )
        return items

    def _fetch_repo_directory_entries(
        self,
        *,
        owner: str,
        name: str,
        directory: str,
        default_branch: str,
    ) -> list[object]:
        encoded_ref = quote(default_branch, safe="")
        if directory:
            encoded_directory = quote(directory, safe="/")
            endpoint = f"repos/{owner}/{name}/contents/{encoded_directory}?ref={encoded_ref}"
        else:
            endpoint = f"repos/{owner}/{name}/contents?ref={encoded_ref}"

        try:
            payload = _run_command_json_any(["gh", "api", endpoint])
        except RuntimeError as error:
            message = str(error)
            if "404" in message or "Not Found" in message:
                return []
            raise

        entries: list[object] = cast("list[object]", payload) if isinstance(payload, list) else [payload]
        out: list[object] = []
        for raw_entry in entries:
            entry = _as_dict_optional(raw_entry)
            if entry is None:
                continue
            path = _normalized_optional_str(entry.get("path"))
            item_type = _normalized_optional_str(entry.get("type"))
            if path is None or item_type is None:
                continue
            normalized_type = "blob" if item_type == "file" else item_type
            out.append({"path": path, "type": normalized_type})
        return out

    def _resolve_default_branch_protection(
        self,
        *,
        owner: str,
        name: str,
        default_branch: str,
    ) -> RepoBranchProtection | None:
        encoded_branch = quote(default_branch, safe="")
        branch_payload = _run_command_json(["gh", "api", f"repos/{owner}/{name}/branches/{encoded_branch}"])
        if not bool(branch_payload.get("protected")):
            return None

        protection_obj = _as_dict_optional(branch_payload.get("protection"))
        required_status_checks_obj = (
            _as_dict_optional(protection_obj.get("required_status_checks")) if protection_obj is not None else None
        )
        required_status_check_contexts = _extract_required_status_check_contexts(required_status_checks_obj)
        requires_status_checks = bool(
            required_status_checks_obj is not None
            and (
                _normalized_optional_str(required_status_checks_obj.get("enforcement_level")) not in {None, "off"}
                or required_status_check_contexts
            )
        )

        rule = self._resolve_default_branch_protection_rule_details(
            owner=owner,
            name=name,
            default_branch=default_branch,
        )
        if rule is not None:
            return RepoBranchProtection(
                pattern=rule.pattern,
                source="graphql",
                requires_status_checks=requires_status_checks,
                required_status_check_contexts=required_status_check_contexts,
                requires_approving_reviews=rule.requires_approving_reviews,
                required_approving_review_count=rule.required_approving_review_count,
                requires_code_owner_reviews=rule.requires_code_owner_reviews,
                is_admin_enforced=rule.is_admin_enforced,
            )

        return RepoBranchProtection(
            pattern=default_branch,
            source="rest",
            requires_status_checks=requires_status_checks,
            required_status_check_contexts=required_status_check_contexts,
        )

    def _resolve_default_branch_protection_rule_details(
        self,
        *,
        owner: str,
        name: str,
        default_branch: str,
    ) -> RepoBranchProtection | None:
        query = """
query($owner:String!,$name:String!,$after:String){
  repository(owner:$owner,name:$name){
    branchProtectionRules(first:100, after:$after){
      pageInfo{hasNextPage endCursor}
      nodes{
        pattern
        requiresStatusChecks
        requiredStatusCheckContexts
        requiresApprovingReviews
        requiredApprovingReviewCount
        requiresCodeOwnerReviews
        isAdminEnforced
      }
    }
  }
}
""".strip()
        after: str | None = None
        best_match: tuple[tuple[int, int, int], RepoBranchProtection] | None = None

        while True:
            variables: dict[str, str | int] = {"owner": owner, "name": name}
            if after is not None:
                variables["after"] = after
            payload = _run_graphql_payload(query, variables)
            data_obj = _as_dict(payload.get("data"), context="graphql data")
            repo_obj = _as_dict(data_obj.get("repository"), context="repository")
            rules_obj = _as_dict(repo_obj.get("branchProtectionRules"), context="branchProtectionRules")

            for raw_rule in _as_list(rules_obj.get("nodes")):
                candidate = _build_branch_protection_rule_candidate(default_branch=default_branch, raw_rule=raw_rule)
                if candidate is None:
                    continue
                if best_match is None or candidate[0] < best_match[0]:
                    best_match = candidate

            page_info = _as_dict(rules_obj.get("pageInfo"), context="branchProtectionRules pageInfo")
            if not bool(page_info.get("hasNextPage")):
                break
            after = _as_optional_str(page_info.get("endCursor"))
            if after is None:
                break

        return None if best_match is None else best_match[1]

    def fetch_timeline_forward(
        self,
        ref: PullRequestRef,
        page_size: int,
        after: str | None,
        *,
        timeline_window: TimelineWindow | None = None,
        show_resolved_details: bool = False,
        show_outdated_details: bool = False,
        show_minimized_details: bool = False,
        show_details_blocks: bool = False,
        review_threads_window: int | None = 10,
        diff_hunk_lines: int | None = DEFAULT_REVIEW_DIFF_HUNK_LINES,
        kind: str = "pr",
    ) -> TimelinePage:
        variables: dict[str, str | int] = {
            "owner": ref.owner,
            "name": ref.name,
            "number": ref.number,
            "pageSize": page_size,
        }
        if after is not None:
            variables["after"] = after

        if kind == "issue":
            connection = _run_graphql_connection(ISSUE_FORWARD_TIMELINE_QUERY, variables, subject_key="issue")
            threads_by_review: dict[str, list[dict[str, object]]] = {}
        else:
            connection = _run_graphql_connection(FORWARD_TIMELINE_QUERY, variables, subject_key="pullRequest")
            threads_by_review = self._get_review_threads_by_review(ref)
        return _parse_timeline_page(
            connection,
            ref=ref,
            threads_by_review=threads_by_review,
            timeline_window=timeline_window,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            viewer_login=self._viewer_login or "",
            subject_kind=kind,
        )

    def fetch_timeline_backward(
        self,
        ref: PullRequestRef,
        page_size: int,
        before: str | None,
        *,
        timeline_window: TimelineWindow | None = None,
        show_resolved_details: bool = False,
        show_outdated_details: bool = False,
        show_minimized_details: bool = False,
        show_details_blocks: bool = False,
        review_threads_window: int | None = 10,
        diff_hunk_lines: int | None = DEFAULT_REVIEW_DIFF_HUNK_LINES,
        kind: str = "pr",
    ) -> TimelinePage:
        variables: dict[str, str | int] = {
            "owner": ref.owner,
            "name": ref.name,
            "number": ref.number,
            "pageSize": page_size,
        }
        if before is not None:
            variables["before"] = before

        if kind == "issue":
            connection = _run_graphql_connection(ISSUE_BACKWARD_TIMELINE_QUERY, variables, subject_key="issue")
            threads_by_review: dict[str, list[dict[str, object]]] = {}
        else:
            connection = _run_graphql_connection(BACKWARD_TIMELINE_QUERY, variables, subject_key="pullRequest")
            threads_by_review = self._get_review_threads_by_review(ref)
        return _parse_timeline_page(
            connection,
            ref=ref,
            threads_by_review=threads_by_review,
            timeline_window=timeline_window,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            viewer_login=self._viewer_login or "",
            subject_kind=kind,
        )

    def _get_review_threads_by_review(self, ref: PullRequestRef) -> dict[str, list[dict[str, object]]]:
        key = (ref.owner, ref.name, ref.number)
        cached = self._review_threads_cache.get(key)
        if cached is not None:
            return cached

        by_review: dict[str, list[dict[str, object]]] = {}
        for raw_thread in self._get_review_threads(ref):
            thread = _as_dict(raw_thread, context="reviewThread")
            thread_id = _as_optional_str(thread.get("id")) or ""
            is_resolved = bool(thread.get("isResolved"))
            comments = [
                _as_dict(comment, context="reviewThread comment") for comment in _as_list(thread.get("comments"))
            ]

            review_ids: set[str] = set()
            for comment in comments:
                review_obj = _as_dict_optional(comment.get("pullRequestReview"))
                review_id = _as_optional_str(review_obj.get("id")) if review_obj is not None else None
                if review_id:
                    review_ids.add(review_id)

            if not review_ids:
                continue

            for review_id in sorted(review_ids):
                thread_payload: dict[str, object] = {
                    "id": thread_id,
                    "isResolved": is_resolved,
                    "comments": comments,
                }
                by_review.setdefault(review_id, []).append(thread_payload)

        self._review_threads_cache[key] = by_review
        return by_review

    def _get_review_threads(self, ref: PullRequestRef) -> tuple[dict[str, object], ...]:
        key = (ref.owner, ref.name, ref.number)
        cached = self._review_threads_raw_cache.get(key)
        if cached is not None:
            return cached

        threads: list[dict[str, object]] = []
        after: str | None = None
        while True:
            variables: dict[str, str | int] = {
                "owner": ref.owner,
                "name": ref.name,
                "number": ref.number,
            }
            if after is not None:
                variables["after"] = after
            payload = _run_graphql_payload(REVIEW_THREADS_QUERY, variables)
            data_obj = _as_dict(payload.get("data"), context="graphql data")
            repo_obj = _as_dict(data_obj.get("repository"), context="repository")
            pr_obj = _as_dict(repo_obj.get("pullRequest"), context="pullRequest")
            threads_obj = _as_dict(pr_obj.get("reviewThreads"), context="reviewThreads")

            for raw_thread in _as_list(threads_obj.get("nodes")):
                thread = _as_dict(raw_thread, context="reviewThread")
                thread_id = _as_optional_str(thread.get("id")) or ""
                is_resolved = bool(thread.get("isResolved"))
                comments_obj = _as_dict_optional(thread.get("comments"))
                if comments_obj is None:
                    continue
                comments = [
                    _as_dict(raw_comment, context="reviewThread comment")
                    for raw_comment in _as_list(comments_obj.get("nodes"))
                ]
                if not comments:
                    continue
                threads.append(
                    {
                        "id": thread_id,
                        "isResolved": is_resolved,
                        "comments": comments,
                    }
                )

            page_info = _as_dict(threads_obj.get("pageInfo"), context="reviewThreads pageInfo")
            has_next = bool(page_info.get("hasNextPage"))
            after = _as_optional_str(page_info.get("endCursor"))
            if not has_next:
                break

        result = tuple(threads)
        self._review_threads_raw_cache[key] = result
        return result

    def fetch_review_thread_summaries(self, ref: PullRequestRef) -> tuple[ReviewThreadSummary, ...]:
        summaries: list[ReviewThreadSummary] = []
        for raw_thread in self._get_review_threads(ref):
            thread = _as_dict(raw_thread, context="reviewThread")
            thread_id = _as_optional_str(thread.get("id")) or ""
            comments = [
                _as_dict(comment, context="reviewThread comment") for comment in _as_list(thread.get("comments"))
            ]
            if not comments:
                continue
            path = _first_non_empty_comment_path(comments)
            if not path:
                continue
            right_lines = _collect_thread_lines(comments, keys=("startLine", "line"))
            left_lines = _collect_thread_lines(comments, keys=("originalStartLine", "originalLine"))
            anchor_side, anchor_line = _resolve_review_thread_anchor(comments)
            summaries.append(
                ReviewThreadSummary(
                    thread_id=thread_id,
                    path=path,
                    is_resolved=bool(thread.get("isResolved")),
                    comment_count=len(comments),
                    is_outdated=_is_review_thread_outdated(comments),
                    anchor_side=anchor_side,
                    anchor_line=anchor_line,
                    right_lines=right_lines,
                    left_lines=left_lines,
                    display_ref=_format_thread_summary_display_ref(right_lines=right_lines, left_lines=left_lines),
                    comments=tuple(_build_review_comment_summaries(comments)),
                )
            )
        return tuple(summaries)

    def expand_review_thread(
        self,
        *,
        ref: PullRequestRef,
        thread_id: str,
        show_details_blocks: bool = False,
        diff_hunk_lines: int | None = DEFAULT_REVIEW_DIFF_HUNK_LINES,
    ) -> tuple[str, list[str]]:
        if self._viewer_login is None:
            self._viewer_login = self._get_viewer_login()
        threads_by_review = self._get_review_threads_by_review(ref)
        for review_id, threads in threads_by_review.items():
            for raw_thread in threads:
                thread = _as_dict(raw_thread, context="review thread")
                current_id = _as_optional_str(thread.get("id")) or ""
                if current_id != thread_id:
                    continue
                lines, _, _, _ = _render_review_thread_block(
                    thread_id=current_id,
                    is_resolved=bool(thread.get("isResolved")),
                    thread_index=1,
                    comments=_as_list(thread.get("comments")),
                    ref=ref,
                    viewer_login=self._viewer_login or "",
                    show_outdated_details=True,
                    show_minimized_details=True,
                    show_details_blocks=show_details_blocks,
                    diff_hunk_lines=diff_hunk_lines,
                )
                return review_id, lines
        raise RuntimeError(f"thread {thread_id} not found on this pull request")

    def expand_review(
        self,
        *,
        ref: PullRequestRef,
        review_id: str,
        thread_start: int | None = None,
        thread_end: int | None = None,
        show_resolved_details: bool = True,
        show_details_blocks: bool = False,
        diff_hunk_lines: int | None = DEFAULT_REVIEW_DIFF_HUNK_LINES,
    ) -> list[str]:
        if self._viewer_login is None:
            self._viewer_login = self._get_viewer_login()
        threads_by_review = self._get_review_threads_by_review(ref)
        threads = list(threads_by_review.get(review_id, []))
        if not threads:
            raise RuntimeError(f"review {review_id} not found on this pull request")

        filtered: list[tuple[int, dict[str, object]]] = []
        logical_index = 0
        for raw_thread in threads:
            thread = _as_dict(raw_thread, context="review thread")
            is_resolved = bool(thread.get("isResolved"))
            if is_resolved and not show_resolved_details:
                continue
            logical_index += 1
            filtered.append((logical_index, thread))

        if thread_start is not None or thread_end is not None:
            start = thread_start or 1
            end = thread_end or len(filtered)
            selected = [item for item in filtered if start <= item[0] <= end]
        else:
            selected = filtered

        if not selected:
            raise RuntimeError("no review conversations matched requested thread range")

        lines: list[str] = []
        visible_comment_count = 0
        total_comment_count = 0
        for logical_idx, thread in selected:
            comments = _as_list(thread.get("comments"))
            total_comment_count += len(comments)
            thread_id = _as_optional_str(thread.get("id")) or "(unknown thread id)"
            thread_lines, visible, _, _ = _render_review_thread_block(
                thread_id=thread_id,
                is_resolved=bool(thread.get("isResolved")),
                thread_index=logical_idx,
                comments=comments,
                ref=ref,
                viewer_login=self._viewer_login or "",
                show_outdated_details=True,
                show_minimized_details=True,
                show_details_blocks=show_details_blocks,
                diff_hunk_lines=diff_hunk_lines,
            )
            lines.extend(thread_lines)
            visible_comment_count += visible

        return [f"Review comments ({visible_comment_count}/{total_comment_count} shown):", *lines]

    def reply_review_thread(self, thread_id: str, body: str) -> str:
        query = """
mutation($threadId:ID!,$body:String!){
  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,body:$body}){
    comment{id}
  }
}
""".strip()
        payload = _run_graphql_payload(query, {"threadId": thread_id, "body": body})
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        reply_obj = _as_dict(data_obj.get("addPullRequestReviewThreadReply"), context="addPullRequestReviewThreadReply")
        comment_obj = _as_dict(reply_obj.get("comment"), context="reply comment")
        return _as_optional_str(comment_obj.get("id")) or ""

    def resolve_review_thread(self, thread_id: str) -> bool:
        query = """
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){
    thread{id isResolved}
  }
}
""".strip()
        payload = _run_graphql_payload(query, {"threadId": thread_id})
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        resolved_obj = _as_dict(data_obj.get("resolveReviewThread"), context="resolveReviewThread")
        thread_obj = _as_dict(resolved_obj.get("thread"), context="resolved thread")
        return bool(thread_obj.get("isResolved"))

    def unresolve_review_thread(self, thread_id: str) -> bool:
        query = """
mutation($threadId:ID!){
  unresolveReviewThread(input:{threadId:$threadId}){
    thread{id isResolved}
  }
}
""".strip()
        payload = _run_graphql_payload(query, {"threadId": thread_id})
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        unresolved_obj = _as_dict(data_obj.get("unresolveReviewThread"), context="unresolveReviewThread")
        thread_obj = _as_dict(unresolved_obj.get("thread"), context="unresolved thread")
        return bool(thread_obj.get("isResolved"))

    def edit_comment(self, comment_id: str, body: str) -> str:
        if comment_id.startswith("PRRC_"):
            updated_id = self._try_update_pull_request_review_comment(comment_id=comment_id, body=body)
            if updated_id:
                return updated_id
            updated_id = self._try_update_issue_comment(comment_id=comment_id, body=body)
            if updated_id:
                return updated_id
            raise RuntimeError("failed to edit review comment")

        updated_id = self._try_update_issue_comment(comment_id=comment_id, body=body)
        if updated_id:
            return updated_id
        updated_id = self._try_update_pull_request_review_comment(comment_id=comment_id, body=body)
        if updated_id:
            return updated_id
        raise RuntimeError("failed to edit comment")

    def fetch_comment_node(self, comment_id: str) -> dict[str, object]:
        payload = _run_graphql_payload(COMMENT_NODE_QUERY, {"id": comment_id})
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        node_obj = _as_dict_optional(data_obj.get("node"))
        if node_obj is None:
            raise RuntimeError(f"comment {comment_id} not found")
        typename = _as_optional_str(node_obj.get("__typename")) or ""
        if typename not in {"IssueComment", "PullRequestReviewComment"}:
            raise RuntimeError(f"unsupported comment type for {comment_id}: {typename or 'unknown'}")
        return node_obj

    def _try_update_issue_comment(self, *, comment_id: str, body: str) -> str | None:
        query = """
mutation($id:ID!,$body:String!){
  updateIssueComment(input:{id:$id,body:$body}){
    issueComment{id}
  }
}
""".strip()
        payload = _run_graphql_payload(query, {"id": comment_id, "body": body})
        if _has_graphql_errors(payload):
            return None
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        updated_obj = _as_dict_optional(data_obj.get("updateIssueComment"))
        if updated_obj is None:
            return None
        comment_obj = _as_dict_optional(updated_obj.get("issueComment"))
        if comment_obj is None:
            return None
        updated_id = _as_optional_str(comment_obj.get("id"))
        return updated_id or None

    def _try_update_pull_request_review_comment(self, *, comment_id: str, body: str) -> str | None:
        query = """
mutation($id:ID!,$body:String!){
  updatePullRequestReviewComment(input:{pullRequestReviewCommentId:$id,body:$body}){
    pullRequestReviewComment{id}
  }
}
""".strip()
        payload = _run_graphql_payload(query, {"id": comment_id, "body": body})
        if _has_graphql_errors(payload):
            return None
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        updated_obj = _as_dict_optional(data_obj.get("updatePullRequestReviewComment"))
        if updated_obj is None:
            return None
        comment_obj = _as_dict_optional(updated_obj.get("pullRequestReviewComment"))
        if comment_obj is None:
            return None
        updated_id = _as_optional_str(comment_obj.get("id"))
        return updated_id or None

    def _get_viewer_login(self) -> str:
        payload = _run_command_json(["gh", "api", "user"])
        login = _as_optional_str(payload.get("login"))
        return login or ""

    def fetch_checks(self, ref: PullRequestRef) -> list[CheckItem]:
        payload = _run_graphql_payload(
            CHECKS_QUERY,
            {"owner": ref.owner, "name": ref.name, "number": ref.number},
        )
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        repo_obj = _as_dict(data_obj.get("repository"), context="repository")
        pr_obj = _as_dict(repo_obj.get("pullRequest"), context="pullRequest")
        commits_obj = _as_dict(pr_obj.get("commits"), context="commits")
        nodes = _as_list(commits_obj.get("nodes"))
        if not nodes:
            return []
        head = _as_dict(nodes[0], context="commit node")
        commit_obj = _as_dict(head.get("commit"), context="commit")
        rollup_obj = _as_dict_optional(commit_obj.get("statusCheckRollup"))
        if rollup_obj is None:
            return []
        contexts_obj = _as_dict_optional(rollup_obj.get("contexts"))
        if contexts_obj is None:
            return []

        items: list[CheckItem] = []
        for raw in _as_list(contexts_obj.get("nodes")):
            node = _as_dict(raw, context="check context")
            typename = _as_optional_str(node.get("__typename")) or ""
            if typename == "CheckRun":
                name = (_as_optional_str(node.get("name")) or "").strip() or "(unnamed check run)"
                status = _as_optional_str(node.get("status")) or "UNKNOWN"
                conclusion = _as_optional_str(node.get("conclusion"))
                label = f"{status}/{(conclusion or 'NONE')}"
                details_url = _as_optional_str(node.get("detailsUrl"))
                run_id, job_id = _extract_actions_run_and_job_ids(details_url)
                items.append(
                    CheckItem(
                        name=name,
                        kind="check-run",
                        status=label,
                        passed=_is_check_run_passed(status=status, conclusion=conclusion),
                        details_url=details_url,
                        run_id=run_id,
                        job_id=job_id,
                    )
                )
                continue
            if typename == "StatusContext":
                name = (_as_optional_str(node.get("context")) or "").strip() or "(unnamed status)"
                state = _as_optional_str(node.get("state")) or "UNKNOWN"
                items.append(
                    CheckItem(
                        name=name,
                        kind="status-context",
                        status=state,
                        passed=(state == "SUCCESS"),
                        details_url=_as_optional_str(node.get("targetUrl")),
                        run_id=None,
                    )
                )
        return items

    def fetch_pr_diff(self, selector: str | None, repo: str | None) -> str:
        cmd = ["gh", "pr", "diff"]
        if selector:
            cmd.append(selector)
        if repo:
            cmd.extend(["--repo", repo])
        return _run_command_text(cmd)

    def fetch_pr_files_page(self, meta: PullRequestMeta, *, page: int, page_size: int) -> PullRequestDiffPage:
        if page < 1:
            raise RuntimeError(f"invalid file page {page}, expected >= 1")
        if page_size < 1 or page_size > 100:
            raise RuntimeError(f"invalid file page size {page_size}, expected in 1..100")

        total_files = meta.changed_files or 0
        total_pages = max(1, (total_files + page_size - 1) // page_size) if total_files > 0 else 1
        if total_files > 0 and page > total_pages:
            raise RuntimeError(f"invalid file page {page}, expected in 1..{total_pages}")

        path = f"repos/{meta.ref.owner}/{meta.ref.name}/pulls/{meta.ref.number}/files?per_page={page_size}&page={page}"
        payload = _run_command_json_any(
            ["gh", "api", path],
            max_attempts=GRAPHQL_MAX_ATTEMPTS,
            backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
            backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
        )
        raw_files = _as_list(payload)
        files: list[PullRequestDiffFile] = []
        for raw_file in raw_files:
            file_obj = _as_dict_optional(raw_file)
            if file_obj is None:
                continue
            path_value = _as_optional_str(file_obj.get("filename"))
            if not path_value:
                continue
            files.append(
                PullRequestDiffFile(
                    path=path_value,
                    status=(_as_optional_str(file_obj.get("status")) or "unknown"),
                    additions=_as_int_default(file_obj.get("additions"), default=0),
                    deletions=_as_int_default(file_obj.get("deletions"), default=0),
                    changes=_as_int_default(file_obj.get("changes"), default=0),
                    patch=_as_optional_str(file_obj.get("patch")),
                    previous_path=_as_optional_str(file_obj.get("previous_filename")),
                )
            )
        return PullRequestDiffPage(
            page=page,
            page_size=page_size,
            total_files=total_files,
            total_pages=total_pages,
            files=tuple(files),
        )

    def fetch_file_lines(self, ref: PullRequestRef, *, path: str, revision: str) -> tuple[str, ...] | None:
        cache_key = (ref.owner, ref.name, revision, path)
        if cache_key in self._file_lines_cache:
            return self._file_lines_cache[cache_key]

        api_path = f"repos/{ref.owner}/{ref.name}/contents/{quote(path, safe='/')}?ref={revision}"
        try:
            payload = _run_command_json(
                ["gh", "api", api_path],
                max_attempts=GRAPHQL_MAX_ATTEMPTS,
                backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
                backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
            )
        except RuntimeError:
            self._file_lines_cache[cache_key] = None
            return None

        decoded = _decode_repository_contents_text(payload)
        if decoded is None:
            self._file_lines_cache[cache_key] = None
            return None

        lines = tuple(decoded.splitlines())
        self._file_lines_cache[cache_key] = lines
        return lines

    def fetch_pull_request_template(self, repo: str) -> tuple[str | None, str | None]:
        owner, name = _parse_repo_full_name(repo)
        self._assert_repository_accessible(owner=owner, name=name)
        for candidate in _iter_direct_pull_request_template_candidate_paths():
            text = self._fetch_repository_text_file(owner=owner, name=name, path=candidate)
            if text is not None:
                return candidate, text

        for parent_path in _PULL_REQUEST_TEMPLATE_PARENT_PATHS:
            candidate = self._find_direct_pull_request_template_via_listing(
                owner=owner,
                name=name,
                parent_path=parent_path,
            )
            if candidate is None:
                continue
            text = self._fetch_repository_text_file(owner=owner, name=name, path=candidate)
            if text is not None:
                return candidate, text

        seen_directories: set[str] = set()
        for parent_path in _PULL_REQUEST_TEMPLATE_PARENT_PATHS:
            for directory in self._list_pull_request_template_directories(
                owner=owner,
                name=name,
                parent_path=parent_path,
            ):
                if directory in seen_directories:
                    continue
                seen_directories.add(directory)
                for candidate in self._list_repository_template_files(owner=owner, name=name, path=directory):
                    text = self._fetch_repository_text_file(owner=owner, name=name, path=candidate)
                    if text is not None:
                        return candidate, text

        return None, None

    def _assert_repository_accessible(self, *, owner: str, name: str) -> None:
        _run_command_json(
            ["gh", "api", f"repos/{owner}/{name}"],
            max_attempts=GRAPHQL_MAX_ATTEMPTS,
            backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
            backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
        )

    def _fetch_repository_text_file(self, *, owner: str, name: str, path: str) -> str | None:
        api_path = _build_repository_contents_api_path(owner=owner, name=name, path=path)
        try:
            payload = _run_command_json(
                ["gh", "api", api_path],
                max_attempts=GRAPHQL_MAX_ATTEMPTS,
                backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
                backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
            )
        except RuntimeError as error:
            if _is_gh_api_not_found_error(str(error)):
                return None
            raise

        if (_as_optional_str(payload.get("type")) or "") != "file":
            return None
        return _decode_repository_contents_text(payload)

    def _find_direct_pull_request_template_via_listing(
        self,
        *,
        owner: str,
        name: str,
        parent_path: str,
    ) -> str | None:
        candidates: list[str] = []
        for entry in self._list_repository_contents(owner=owner, name=name, path=parent_path):
            if (_as_optional_str(entry.get("type")) or "") != "file":
                continue
            entry_name = _as_optional_str(entry.get("name")) or ""
            if not _is_direct_pull_request_template_name(entry_name):
                continue
            candidate_path = _as_optional_str(entry.get("path")) or ""
            if candidate_path:
                candidates.append(candidate_path)
        if not candidates:
            return None
        return sorted(candidates, key=str.casefold)[0]

    def _list_pull_request_template_directories(
        self,
        *,
        owner: str,
        name: str,
        parent_path: str,
    ) -> tuple[str, ...]:
        candidates: list[str] = []
        for entry in self._list_repository_contents(owner=owner, name=name, path=parent_path):
            if (_as_optional_str(entry.get("type")) or "") != "dir":
                continue
            entry_name = _as_optional_str(entry.get("name")) or ""
            if not _is_pull_request_template_directory_name(entry_name):
                continue
            candidate_path = _as_optional_str(entry.get("path")) or ""
            if candidate_path:
                candidates.append(candidate_path)
        return tuple(sorted(candidates, key=str.casefold))

    def _list_repository_contents(self, *, owner: str, name: str, path: str) -> tuple[dict[str, object], ...]:
        api_path = _build_repository_contents_api_path(owner=owner, name=name, path=path)
        try:
            payload = _run_command_json_any(
                ["gh", "api", api_path],
                max_attempts=GRAPHQL_MAX_ATTEMPTS,
                backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
                backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
            )
        except RuntimeError as error:
            if _is_gh_api_not_found_error(str(error)):
                return ()
            raise

        entries: list[dict[str, object]] = []
        for raw_entry in _as_list(payload):
            entry = _as_dict_optional(raw_entry)
            if entry is None:
                continue
            entries.append(entry)
        return tuple(entries)

    def _list_repository_template_files(self, *, owner: str, name: str, path: str) -> tuple[str, ...]:
        candidates: list[str] = []
        for entry in self._list_repository_contents(owner=owner, name=name, path=path):
            if (_as_optional_str(entry.get("type")) or "") != "file":
                continue
            candidate_path = _as_optional_str(entry.get("path")) or ""
            if not _is_pull_request_template_path(candidate_path):
                continue
            candidates.append(candidate_path)
        return tuple(sorted(candidates, key=str.casefold))

    def submit_pull_request_review(
        self,
        *,
        ref: PullRequestRef,
        event: str,
        body: str | None = None,
    ) -> tuple[str, str]:
        pending_review_id = self._find_pending_review_id(ref)
        if pending_review_id is not None:
            payload = _run_graphql_payload_any(
                SUBMIT_PULL_REQUEST_REVIEW_QUERY,
                {
                    "id": pending_review_id,
                    "event": event,
                    "body": body or "",
                },
            )
            data_obj = _as_dict(payload.get("data"), context="graphql data")
            submitted_obj = _as_dict(data_obj.get("submitPullRequestReview"), context="submitPullRequestReview")
            review_obj = _as_dict(submitted_obj.get("pullRequestReview"), context="pullRequestReview")
            review_id = _as_optional_str(review_obj.get("id")) or ""
            review_state = _as_optional_str(review_obj.get("state")) or ""
            if not review_id:
                raise RuntimeError("failed to submit pending pull request review")
            return review_id, review_state

        pr_id = self._resolve_pull_request_node_id(ref)
        variables: dict[str, object] = {"pullRequestId": pr_id, "event": event}
        if body is not None:
            variables["body"] = body
        review_payload = _run_graphql_payload_any(ADD_PULL_REQUEST_REVIEW_QUERY, variables)
        review_data_obj = _as_dict(review_payload.get("data"), context="graphql data")
        added_obj = _as_dict(review_data_obj.get("addPullRequestReview"), context="addPullRequestReview")
        review_obj = _as_dict(added_obj.get("pullRequestReview"), context="pullRequestReview")
        review_id = _as_optional_str(review_obj.get("id")) or ""
        review_state = _as_optional_str(review_obj.get("state")) or ""
        if not review_id:
            raise RuntimeError("failed to create pull request review")
        return review_id, review_state

    def _find_pending_review_id(self, ref: PullRequestRef) -> str | None:
        viewer = self._viewer_login
        if viewer is None:
            viewer = self._get_viewer_login()
            self._viewer_login = viewer
        payload = _run_graphql_payload(
            PENDING_REVIEWS_QUERY,
            {"owner": ref.owner, "name": ref.name, "number": ref.number},
        )
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        repo_obj = _as_dict(data_obj.get("repository"), context="repository")
        pr_obj = _as_dict(repo_obj.get("pullRequest"), context="pullRequest")
        reviews_obj = _as_dict(pr_obj.get("reviews"), context="reviews")
        for raw in _as_list(reviews_obj.get("nodes")):
            review = _as_dict(raw, context="review")
            state = _as_optional_str(review.get("state")) or ""
            author_login = _get_login(review.get("author"))
            review_id = _as_optional_str(review.get("id")) or ""
            if state == "PENDING" and author_login == viewer and review_id:
                return review_id
        return None

    def add_pull_request_review_thread_comment(
        self,
        *,
        ref: PullRequestRef,
        path: str,
        line: int,
        side: str,
        body: str,
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> tuple[str, str]:
        pr_id = self._resolve_pull_request_node_id(ref)
        variables: dict[str, object] = {
            "pullRequestId": pr_id,
            "path": path,
            "line": line,
            "side": side,
            "body": body,
        }
        if start_line is not None:
            variables["startLine"] = start_line
            variables["startSide"] = side if start_side is None else start_side
        payload = _run_graphql_payload_any(
            ADD_PULL_REQUEST_REVIEW_THREAD_QUERY,
            variables,
        )
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        added_obj = _as_dict(data_obj.get("addPullRequestReviewThread"), context="addPullRequestReviewThread")
        thread_obj = _as_dict_optional(added_obj.get("thread"))
        if thread_obj is None:
            raise RuntimeError(
                "failed to create review thread: GitHub rejected the requested review location "
                f"({path}:{line} {side}). The line may be outside the current PR diff or otherwise not commentable."
            )
        thread_id = _as_optional_str(thread_obj.get("id")) or ""
        comments_obj = _as_dict(thread_obj.get("comments"), context="thread comments")
        comment_nodes = _as_list(comments_obj.get("nodes"))
        comment_id = ""
        if comment_nodes:
            first = _as_dict(comment_nodes[0], context="thread comment")
            comment_id = _as_optional_str(first.get("id")) or ""
        if not thread_id:
            raise RuntimeError("failed to create review thread comment")
        return thread_id, comment_id

    def _resolve_pull_request_node_id(self, ref: PullRequestRef) -> str:
        payload = _run_graphql_payload(
            PR_NODE_ID_QUERY,
            {"owner": ref.owner, "name": ref.name, "number": ref.number},
        )
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        repo_obj = _as_dict(data_obj.get("repository"), context="repository")
        pr_obj = _as_dict(repo_obj.get("pullRequest"), context="pullRequest")
        pr_id = _as_optional_str(pr_obj.get("id")) or ""
        if not pr_id:
            raise RuntimeError("failed to resolve pull request node id")
        return pr_id

    def _fetch_pull_request_actions_meta(self, ref: PullRequestRef) -> dict[str, object]:
        payload = _run_graphql_payload(
            PULL_REQUEST_ACTIONS_META_QUERY,
            {"owner": ref.owner, "name": ref.name, "number": ref.number},
        )
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        repo_obj = _as_dict(data_obj.get("repository"), context="repository")
        pr_obj = _as_dict(repo_obj.get("pullRequest"), context="pullRequest")
        out: dict[str, object] = dict(pr_obj)
        out["mergeCommitAllowed"] = repo_obj.get("mergeCommitAllowed")
        out["squashMergeAllowed"] = repo_obj.get("squashMergeAllowed")
        out["rebaseMergeAllowed"] = repo_obj.get("rebaseMergeAllowed")
        return out

    def _is_head_ref_deleted(self, *, head_repo: str | None, head_ref_name: str | None) -> bool:
        if not head_repo or not head_ref_name:
            return True
        if "/" not in head_repo:
            return True
        owner, name = head_repo.split("/", 1)
        payload = _run_graphql_payload(
            REPOSITORY_REF_EXISTS_QUERY,
            {"owner": owner, "name": name, "qualifiedName": f"refs/heads/{head_ref_name}"},
        )
        data_obj = _as_dict(payload.get("data"), context="graphql data")
        repo_obj = _as_dict(data_obj.get("repository"), context="repository")
        return repo_obj.get("ref") is None

    def _detect_conflict_files(
        self,
        *,
        base_repo: str,
        base_ref_name: str | None,
        base_ref_oid: str | None,
        head_repo: str,
        head_ref_name: str | None,
        head_ref_oid: str | None,
    ) -> tuple[str, ...]:
        if not base_ref_name or not head_ref_name:
            return ()
        origin_url = f"https://github.com/{base_repo}.git"
        head_url = f"https://github.com/{head_repo}.git"
        with tempfile.TemporaryDirectory(prefix="gh-llm-merge-conflict-") as temp_dir:
            if not _run_plain_command(["git", "init", "-q"], cwd=temp_dir):
                return ()
            if not _run_plain_command(["git", "remote", "add", "origin", origin_url], cwd=temp_dir):
                return ()
            base_tracking = f"refs/remotes/origin/{base_ref_name}"
            if not _run_plain_command(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "--filter=blob:none",
                    "origin",
                    f"+refs/heads/{base_ref_name}:{base_tracking}",
                ],
                cwd=temp_dir,
            ):
                return ()
            head_remote = "origin"
            if head_repo != base_repo:
                if not _run_plain_command(["git", "remote", "add", "head", head_url], cwd=temp_dir):
                    return ()
                head_remote = "head"
            if not _run_plain_command(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "--filter=blob:none",
                    head_remote,
                    f"+refs/heads/{head_ref_name}:refs/remotes/{head_remote}/{head_ref_name}",
                ],
                cwd=temp_dir,
            ):
                return ()
            base_ref = f"refs/remotes/origin/{base_ref_name}"
            head_ref = f"refs/remotes/{head_remote}/{head_ref_name}"
            merge_tree = subprocess.run(
                ["git", "merge-tree", "--write-tree", "--messages", base_ref, head_ref],
                check=False,
                capture_output=True,
                text=True,
                cwd=temp_dir,
            )
            merge_tree_output = "\n".join([merge_tree.stdout, merge_tree.stderr])
            merge_tree_files = _parse_conflict_files_from_git_output(merge_tree_output)
            if merge_tree_files:
                return tuple(merge_tree_files)
            merge_tree_names = subprocess.run(
                ["git", "merge-tree", "--write-tree", "--name-only", base_ref, head_ref],
                check=False,
                capture_output=True,
                text=True,
                cwd=temp_dir,
            )
            name_only_files = _parse_merge_tree_name_only_output(merge_tree_names.stdout)
            if name_only_files:
                return tuple(name_only_files)
            return ()

    def fetch_conflict_files(self, meta: PullRequestMeta) -> tuple[str, ...]:
        if meta.state != "OPEN":
            return ()
        return self._detect_conflict_files(
            base_repo=f"{meta.ref.owner}/{meta.ref.name}",
            base_ref_name=meta.base_ref_name,
            base_ref_oid=meta.base_ref_oid,
            head_repo=(meta.head_ref_repo or f"{meta.ref.owner}/{meta.ref.name}"),
            head_ref_name=meta.head_ref_name,
            head_ref_oid=meta.head_ref_oid,
        )


def _first_non_empty_comment_path(comments: list[dict[str, object]]) -> str | None:
    for comment in comments:
        path = _as_optional_str(comment.get("path"))
        if path:
            return path
    return None


def _collect_thread_lines(comments: list[dict[str, object]], *, keys: tuple[str, ...]) -> tuple[int, ...]:
    lines: set[int] = set()
    for comment in comments:
        for key in keys:
            value = _as_optional_int(comment.get(key))
            if value is not None and value > 0:
                lines.add(value)
    return tuple(sorted(lines))


def _format_thread_summary_display_ref(
    *,
    right_lines: tuple[int, ...],
    left_lines: tuple[int, ...],
) -> str | None:
    if right_lines:
        return _format_thread_summary_span("R", right_lines)
    if left_lines:
        return _format_thread_summary_span("L", left_lines)
    return None


def _format_thread_summary_span(prefix: str, lines: tuple[int, ...]) -> str:
    if len(lines) == 1:
        return f"{prefix}{lines[0]}"
    return f"{prefix}{lines[0]}-{lines[-1]}"


def _build_review_comment_summaries(comments: list[dict[str, object]]) -> list[ReviewCommentSummary]:
    summaries: list[ReviewCommentSummary] = []
    for comment in comments:
        summaries.append(
            ReviewCommentSummary(
                comment_id=_as_optional_str(comment.get("id")) or "",
                author=_get_login(comment.get("author")),
                body_preview=_build_review_comment_preview(comment),
                is_outdated=bool(comment.get("outdated")) or bool(comment.get("isOutdated")),
                is_minimized=bool(comment.get("isMinimized")),
                minimized_reason=_as_optional_str(comment.get("minimizedReason")),
            )
        )
    return summaries


def _is_review_thread_outdated(comments: list[dict[str, object]]) -> bool:
    return bool(comments) and all(
        bool(comment.get("outdated")) or bool(comment.get("isOutdated")) for comment in comments
    )


def _resolve_review_thread_anchor(comments: list[dict[str, object]]) -> tuple[str | None, int | None]:
    for comment in comments:
        start_line = _as_optional_int(comment.get("startLine"))
        line = _as_optional_int(comment.get("line"))
        if start_line is not None and start_line > 0:
            return "RIGHT", start_line
        if line is not None and line > 0:
            return "RIGHT", line
        original_start_line = _as_optional_int(comment.get("originalStartLine"))
        original_line = _as_optional_int(comment.get("originalLine"))
        if original_start_line is not None and original_start_line > 0:
            return "LEFT", original_start_line
        if original_line is not None and original_line > 0:
            return "LEFT", original_line
    return None, None


def _build_review_comment_preview(comment: dict[str, object]) -> str:
    if bool(comment.get("isMinimized")):
        reason = (_as_optional_str(comment.get("minimizedReason")) or "minimized").lower()
        return f"(hidden comment: {reason})"

    body = _as_optional_str(comment.get("body")) or ""
    plain_body = _strip_suggestion_blocks(body)
    plain_lines = [" ".join(line.strip().split()) for line in plain_body.splitlines() if line.strip()]
    suggestion_lines = [" ".join(line.strip().split()) for line in _extract_suggestion_lines(body) if line.strip()]

    preview = "(no comment body)"
    has_more = False
    if plain_lines:
        preview = plain_lines[0]
        has_more = len(plain_lines) > 1 or bool(suggestion_lines)
    elif suggestion_lines:
        preview = f"suggestion: {suggestion_lines[0]}"
        has_more = len(suggestion_lines) > 1

    if has_more and not preview.endswith(" ..."):
        preview = f"{preview} ..."

    if len(preview) > 140:
        preview = preview[:137].rstrip() + "..."
    return preview


def _run_graphql_connection(
    query: str, variables: dict[str, str | int], *, subject_key: str = "pullRequest"
) -> dict[str, object]:
    payload = _run_graphql_payload(query, variables)
    data_obj = _as_dict(payload.get("data"), context="graphql data")
    repo_obj = _as_dict(data_obj.get("repository"), context="repository")
    subject_obj = _as_dict(repo_obj.get(subject_key), context=subject_key)
    return _as_dict(subject_obj.get("timelineItems"), context="timelineItems")


def _run_graphql_payload(query: str, variables: dict[str, str | int]) -> dict[str, object]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        cmd.extend(["-F", f"{key}={value}"])
    return _run_command_json(
        cmd,
        max_attempts=GRAPHQL_MAX_ATTEMPTS,
        backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
        backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
    )


def _run_graphql_payload_any(query: str, variables: dict[str, object]) -> dict[str, object]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if isinstance(value, (dict, list)):
            cmd.extend(["-F", f"{key}={json.dumps(value, ensure_ascii=False)}"])
        else:
            cmd.extend(["-F", f"{key}={value}"])
    return _run_command_json(
        cmd,
        max_attempts=GRAPHQL_MAX_ATTEMPTS,
        backoff_base_seconds=GRAPHQL_BACKOFF_BASE_SECONDS,
        backoff_max_seconds=GRAPHQL_BACKOFF_MAX_SECONDS,
    )


def _run_command_json(
    cmd: list[str],
    *,
    max_attempts: int = 1,
    backoff_base_seconds: float = 0.0,
    backoff_max_seconds: float = 0.0,
) -> dict[str, object]:
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            parsed: object = json.loads(result.stdout)
            if not isinstance(parsed, dict):
                raise RuntimeError("unexpected non-object JSON response")
            raw = cast("dict[object, object]", parsed)
            return {str(k): v for k, v in raw.items()}

        stderr = result.stderr.strip()
        if attempt >= attempts or not _is_retryable_gh_error(stderr):
            raise GhCommandError(
                cmd=cmd,
                stderr=stderr,
                stdout=result.stdout,
                attempts=attempt,
                max_attempts=attempts,
            )
        delay = min(backoff_max_seconds, backoff_base_seconds * (2 ** (attempt - 1)))
        if delay > 0:
            time.sleep(delay)

    raise GhCommandError(cmd=cmd, stderr="", attempts=attempts, max_attempts=attempts)


def _run_command_json_any(
    cmd: list[str],
    *,
    max_attempts: int = 1,
    backoff_base_seconds: float = 0.0,
    backoff_max_seconds: float = 0.0,
) -> object:
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)

        stderr = result.stderr.strip()
        if attempt >= attempts or not _is_retryable_gh_error(stderr):
            raise GhCommandError(
                cmd=cmd,
                stderr=stderr,
                stdout=result.stdout,
                attempts=attempt,
                max_attempts=attempts,
            )
        delay = min(backoff_max_seconds, backoff_base_seconds * (2 ** (attempt - 1)))
        if delay > 0:
            time.sleep(delay)

    raise GhCommandError(cmd=cmd, stderr="", attempts=attempts, max_attempts=attempts)


def _run_command_text(
    cmd: list[str],
    *,
    max_attempts: int = 1,
    backoff_base_seconds: float = 0.0,
    backoff_max_seconds: float = 0.0,
) -> str:
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr.strip()
        if attempt >= attempts or not _is_retryable_gh_error(stderr):
            raise GhCommandError(
                cmd=cmd,
                stderr=stderr,
                stdout=result.stdout,
                attempts=attempt,
                max_attempts=attempts,
            )
        delay = min(backoff_max_seconds, backoff_base_seconds * (2 ** (attempt - 1)))
        if delay > 0:
            time.sleep(delay)
    raise GhCommandError(cmd=cmd, stderr="", attempts=attempts, max_attempts=attempts)


def _parse_timeline_page(
    connection: dict[str, object],
    *,
    ref: PullRequestRef,
    threads_by_review: dict[str, list[dict[str, object]]],
    timeline_window: TimelineWindow | None,
    show_resolved_details: bool,
    show_outdated_details: bool,
    show_minimized_details: bool,
    show_details_blocks: bool,
    review_threads_window: int | None,
    diff_hunk_lines: int | None,
    viewer_login: str,
    subject_kind: str = "pr",
) -> TimelinePage:
    total_count = _as_int_default(connection.get("totalCount"), default=0)
    page_info_obj = _as_dict(connection.get("pageInfo"), context="pageInfo")
    page_info = PageInfo(
        has_next_page=bool(page_info_obj.get("hasNextPage")),
        has_previous_page=bool(page_info_obj.get("hasPreviousPage")),
        start_cursor=_as_optional_str(page_info_obj.get("startCursor")),
        end_cursor=_as_optional_str(page_info_obj.get("endCursor")),
    )

    items: list[TimelineEvent] = []
    for node in _as_list(connection.get("nodes")):
        parsed = _parse_node(
            _as_dict(node, context="timeline node"),
            ref=ref,
            threads_for_review=threads_by_review,
            timeline_window=timeline_window,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            viewer_login=viewer_login,
            subject_kind=subject_kind,
        )
        if parsed is not None:
            items.append(parsed)

    items.sort(key=lambda value: value.timestamp)
    return TimelinePage(items=items, total_count=total_count, page_info=page_info)


def _parse_node(
    node: dict[str, object],
    *,
    ref: PullRequestRef,
    threads_for_review: dict[str, list[dict[str, object]]],
    timeline_window: TimelineWindow | None,
    show_resolved_details: bool,
    show_outdated_details: bool,
    show_minimized_details: bool,
    show_details_blocks: bool,
    review_threads_window: int | None,
    diff_hunk_lines: int | None,
    viewer_login: str,
    subject_kind: str,
) -> TimelineEvent | None:
    typename = str(node.get("__typename") or "")
    if typename == "IssueComment":
        body = _as_optional_str(node.get("body"))
        details_collapsed_count = 0
        display_body = body
        if not show_details_blocks:
            display_body, details_collapsed_count = _collapse_details_blocks(body)
        is_minimized = bool(node.get("isMinimized"))
        minimized_reason = _format_minimized_reason(node.get("minimizedReason"))
        if is_minimized and not show_minimized_details:
            summary = f"(comment hidden: {minimized_reason})"
            is_truncated = True
        else:
            summary, is_truncated = _clip_text(display_body, "(no comment body)")
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="comment",
            actor=_get_actor_display(node.get("author")),
            summary=summary,
            source_id=_as_optional_str(node.get("id")) or "comment",
            full_text=display_body,
            is_truncated=is_truncated,
            editable_comment_id=(
                _as_optional_str(node.get("id")) if _get_login(node.get("author")) == viewer_login else None
            ),
            reactions_summary=_format_reactions(node.get("reactionGroups")),
            details_collapsed_count=details_collapsed_count,
        )

    if typename == "PullRequestReview":
        state = _as_optional_str(node.get("state")) or "COMMENTED"
        review_id = _as_optional_str(node.get("id")) or "review"
        is_minimized = bool(node.get("isMinimized"))
        minimized_reason = _format_minimized_reason(node.get("minimizedReason"))
        if is_minimized and not show_minimized_details:
            return TimelineEvent(
                timestamp=_parse_datetime(_as_optional_str(node.get("submittedAt"))),
                kind=f"review/{state.lower()}",
                actor=_get_actor_display(node.get("author")),
                summary=f"(review hidden: {minimized_reason})",
                source_id=review_id,
                full_text=_as_optional_str(node.get("body")),
                is_truncated=True,
                minimized_hidden_count=1,
                minimized_hidden_reasons=minimized_reason,
            )
        full_review, resolved_hidden_count, has_clipped_diff_hunk, details_collapsed_count = _build_review_text(
            node=node,
            ref=ref,
            state=state,
            threads_for_review=threads_for_review.get(review_id, []),
            timeline_window=timeline_window,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            viewer_login=viewer_login,
        )
        minimized_hidden_count, minimized_hidden_reasons = _build_review_minimized_summary(
            threads_for_review=threads_for_review.get(review_id, []),
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
        )
        # Review content already uses structured folding (thread window, hidden comments, diff hunk window).
        # Avoid additional character-level clipping that can cut a thread in the middle.
        summary = full_review
        is_truncated = False
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("submittedAt"))),
            kind=f"review/{state.lower()}",
            actor=_get_actor_display(node.get("author")),
            summary=summary,
            source_id=review_id,
            full_text=full_review,
            related_timestamps=_collect_review_comment_timestamps(threads_for_review.get(review_id, [])),
            is_truncated=has_clipped_diff_hunk,
            resolved_hidden_count=resolved_hidden_count,
            minimized_hidden_count=minimized_hidden_count,
            minimized_hidden_reasons=minimized_hidden_reasons,
            details_collapsed_count=details_collapsed_count,
        )

    if typename == "ReviewDismissedEvent":
        review_obj = _as_dict_optional(node.get("review"))
        review_author = _get_actor_display(review_obj.get("author")) if review_obj is not None else "unknown"
        actor = _get_actor_display(node.get("actor"))
        dismissal_message = (_as_optional_str(node.get("dismissalMessage")) or "").strip()
        submitted_at = (_as_optional_str(review_obj.get("submittedAt")) if review_obj is not None else "") or ""
        if actor == review_author and actor != "unknown":
            summary_lines = ["dismissed their stale review"]
        elif review_author != "unknown":
            summary_lines = [f"dismissed stale review from @{review_author}"]
        else:
            summary_lines = ["dismissed stale review"]
        if submitted_at:
            summary_lines.append(f"original review submitted at {submitted_at}")
        if dismissal_message:
            summary_lines.append(f"message: {dismissal_message}")
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="review/dismissed",
            actor=actor,
            summary="\n".join(summary_lines),
            source_id=_as_optional_str(node.get("id")) or "review/dismissed",
            full_text="\n".join(summary_lines),
        )

    if typename == "PullRequestCommit":
        commit = _as_dict(node.get("commit"), context="commit")
        full_message = _as_optional_str(commit.get("message"))
        message = _first_non_empty_line(full_message) or _as_optional_str(commit.get("messageHeadline"))
        oid = _as_optional_str(commit.get("oid")) or "commit"
        actor = _get_commit_actor(commit)
        summary, is_truncated = _clip_text(message, "(empty commit message)")
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(commit.get("committedDate"))),
            kind="push/commit",
            actor=actor,
            summary=summary,
            source_id=oid,
            full_text=full_message or message,
            is_truncated=is_truncated,
        )

    if typename == "ReferencedEvent":
        source = _as_dict_optional(node.get("subject"))
        subject = _reference_subject_summary(source)

        is_cross = bool(node.get("isCrossRepository"))
        actor = _get_actor_display(node.get("actor"))
        summary_lines: list[str] = []
        if subject is not None and subject.type == "PullRequest":
            source_number = subject.number
            source_repo = subject.repo
            detail = subject.detail
            summary_lines.append(f"referenced by PR #{source_number} {detail} ({source_repo})")
            summary_lines.append(f"⏎ view: `{display_command_with(f'pr view {source_number} --repo {source_repo}')}`")
        elif subject is not None and subject.type == "Issue":
            source_number = subject.number
            source_repo = subject.repo
            detail = subject.detail
            summary_lines.append(f"referenced by issue #{source_number} {detail} ({source_repo})")
            summary_lines.append(
                f"⏎ view (reserved): `{display_command_with(f'issue view {source_number} --repo {source_repo}')}`"
            )
        else:
            summary_lines.append("referenced by another item")
        if is_cross:
            summary_lines.append("cross-repository reference")
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="reference",
            actor=actor,
            summary="\n".join(summary_lines),
            source_id=_as_optional_str(node.get("id")) or "referenced",
        )

    if typename == "CrossReferencedEvent":
        source = _as_dict_optional(node.get("source"))
        subject = _reference_subject_summary(source)

        is_cross = bool(node.get("isCrossRepository"))
        actor = _get_actor_display(node.get("actor"))
        summary_lines: list[str] = []
        if subject is not None and subject.type == "PullRequest":
            source_number = subject.number
            source_repo = subject.repo
            detail = subject.detail
            summary_lines.append(f"cross-referenced by PR #{source_number} {detail} ({source_repo})")
            summary_lines.append(f"⏎ view: `{display_command_with(f'pr view {source_number} --repo {source_repo}')}`")
        elif subject is not None and subject.type == "Issue":
            source_number = subject.number
            source_repo = subject.repo
            detail = subject.detail
            summary_lines.append(f"cross-referenced by issue #{source_number} {detail} ({source_repo})")
            summary_lines.append(
                f"⏎ view (reserved): `{display_command_with(f'issue view {source_number} --repo {source_repo}')}`"
            )
        else:
            summary_lines.append("cross-referenced by another item")
        if is_cross:
            summary_lines.append("cross-repository reference")
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="cross-reference",
            actor=actor,
            summary="\n".join(summary_lines),
            source_id=_as_optional_str(node.get("id")) or "cross-referenced",
        )

    if typename == "LabeledEvent":
        label_obj = _as_dict_optional(node.get("label"))
        label_name = _as_optional_str(label_obj.get("name")) if label_obj is not None else None
        label = label_name or "(unknown label)"
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="label/add",
            actor=_get_actor_display(node.get("actor")),
            summary=f"added label `{label}`",
            source_id=_as_optional_str(node.get("id")) or "label/add",
        )

    if typename == "UnlabeledEvent":
        label_obj = _as_dict_optional(node.get("label"))
        label_name = _as_optional_str(label_obj.get("name")) if label_obj is not None else None
        label = label_name or "(unknown label)"
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="label/remove",
            actor=_get_actor_display(node.get("actor")),
            summary=f"removed label `{label}`",
            source_id=_as_optional_str(node.get("id")) or "label/remove",
        )

    if typename == "RenamedTitleEvent":
        previous_title = (_as_optional_str(node.get("previousTitle")) or "").strip()
        current_title = (_as_optional_str(node.get("currentTitle")) or "").strip()
        if previous_title and current_title:
            summary = f"title changed\nfrom: {previous_title}\nto: {current_title}"
        elif current_title:
            summary = f"title changed\nto: {current_title}"
        else:
            summary = "title changed"
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind=f"{subject_kind}/title-edited",
            actor=_get_actor_display(node.get("actor")),
            summary=summary,
            source_id=_as_optional_str(node.get("id")) or f"{subject_kind}/title-edited",
        )

    if typename == "MarkedAsDuplicateEvent":
        actor = _get_actor_display(node.get("actor"))
        is_cross = bool(node.get("isCrossRepository"))
        canonical = _reference_subject_summary(_as_dict_optional(node.get("canonical")))
        duplicate = _reference_subject_summary(_as_dict_optional(node.get("duplicate")))
        lines: list[str] = []
        if canonical is not None and duplicate is not None:
            if canonical.number == ref.number and canonical.repo == f"{ref.owner}/{ref.name}":
                if duplicate.type == "PullRequest":
                    lines.append(
                        f"marked PR #{duplicate.number} {duplicate.detail} ({duplicate.repo}) as duplicate of this issue"
                    )
                    lines.append(
                        f"⏎ view: `{display_command_with(f'pr view {duplicate.number} --repo {duplicate.repo}')}`"
                    )
                else:
                    lines.append(
                        f"marked issue #{duplicate.number} {duplicate.detail} ({duplicate.repo}) as duplicate of this issue"
                    )
                    lines.append(
                        f"⏎ view: `{display_command_with(f'issue view {duplicate.number} --repo {duplicate.repo}')}`"
                    )
            else:
                if canonical.type == "PullRequest":
                    lines.append(
                        f"marked this item as duplicate of PR #{canonical.number} {canonical.detail} ({canonical.repo})"
                    )
                    lines.append(
                        f"⏎ view: `{display_command_with(f'pr view {canonical.number} --repo {canonical.repo}')}`"
                    )
                else:
                    lines.append(
                        f"marked this item as duplicate of issue #{canonical.number} {canonical.detail} ({canonical.repo})"
                    )
                    lines.append(
                        f"⏎ view: `{display_command_with(f'issue view {canonical.number} --repo {canonical.repo}')}`"
                    )
        else:
            lines.append("marked duplicate relationship")
        if is_cross:
            lines.append("cross-repository duplicate marker")
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind=f"{subject_kind}/marked-as-duplicate",
            actor=actor,
            summary="\n".join(lines),
            source_id=_as_optional_str(node.get("id")) or f"{subject_kind}/marked-as-duplicate",
        )

    if typename == "HeadRefForcePushedEvent":
        before_obj = _as_dict_optional(node.get("beforeCommit"))
        after_obj = _as_dict_optional(node.get("afterCommit"))
        ref_obj = _as_dict_optional(node.get("ref"))
        before = (_as_optional_str(before_obj.get("oid")) if before_obj is not None else "") or "unknown"
        after = (_as_optional_str(after_obj.get("oid")) if after_obj is not None else "") or "unknown"
        ref_name = (_as_optional_str(ref_obj.get("name")) if ref_obj is not None else "") or "unknown"
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="push/force",
            actor=_get_actor_display(node.get("actor")),
            summary=f"force-pushed `{ref_name}`\n{before[:7]} -> {after[:7]}",
            source_id=_as_optional_str(node.get("id")) or "push/force",
        )

    if typename == "MergedEvent":
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind="pr/merged",
            actor=_get_actor_display(node.get("actor")),
            summary="pull request merged",
            source_id=_as_optional_str(node.get("id")) or "pr/merged",
        )

    if typename in {"ClosedEvent", "ReopenedEvent"}:
        noun = "issue" if subject_kind == "issue" else "pull request"
        kind_map = {
            "ClosedEvent": f"{subject_kind}/closed",
            "ReopenedEvent": f"{subject_kind}/reopened",
        }
        summary_map = {
            "ClosedEvent": f"{noun} closed",
            "ReopenedEvent": f"{noun} reopened",
        }
        return TimelineEvent(
            timestamp=_parse_datetime(_as_optional_str(node.get("createdAt"))),
            kind=kind_map[typename],
            actor=_get_actor_display(node.get("actor")),
            summary=summary_map[typename],
            source_id=_as_optional_str(node.get("id")) or kind_map[typename],
        )

    return None


def _get_commit_actor(commit: dict[str, object]) -> str:
    authors = _as_dict(commit.get("authors"), context="commit authors")
    nodes = _as_list(authors.get("nodes"))
    if not nodes:
        return "unknown"
    first = _as_dict(nodes[0], context="commit author")
    user = _as_dict_optional(first.get("user"))
    if user is not None:
        login = _as_optional_str(user.get("login"))
        if login:
            return login
    name = _as_optional_str(first.get("name"))
    return name or "unknown"


def _get_login(value: object) -> str:
    obj = _as_dict_optional(value)
    if obj is None:
        return "unknown"
    login = _as_optional_str(obj.get("login"))
    return login or "unknown"


def _get_actor_display(value: object) -> str:
    obj = _as_dict_optional(value)
    if obj is None:
        return "unknown"
    login = _as_optional_str(obj.get("login")) or "unknown"
    name = _as_optional_str(obj.get("name"))
    if name:
        normalized_name = name.strip()
        if normalized_name and normalized_name != login:
            return f"{login} ({normalized_name})"
    return login


def _parse_owner_repo(pr_url: str) -> tuple[str, str]:
    parsed = urlparse(pr_url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) < 2:
        raise RuntimeError(f"failed to parse owner/repo from url: {pr_url}")
    return parts[0], parts[1]


def _parse_repo_full_name(repo: str) -> tuple[str, str]:
    owner, separator, name = repo.strip().partition("/")
    if not owner or not separator or not name:
        raise RuntimeError(f"invalid repo format: {repo}. Expected OWNER/REPO")
    return owner, name


_PULL_REQUEST_TEMPLATE_PARENT_PATHS = (".github", "", "docs")
_DIRECT_PULL_REQUEST_TEMPLATE_BASENAME_VARIANTS = ("PULL_REQUEST_TEMPLATE", "pull_request_template")
_PULL_REQUEST_TEMPLATE_FILE_SUFFIXES = (".md", ".txt", ".markdown", ".mdown")
_DIRECT_PULL_REQUEST_TEMPLATE_FILENAMES = frozenset(
    f"{basename}{suffix}".casefold()
    for basename in _DIRECT_PULL_REQUEST_TEMPLATE_BASENAME_VARIANTS
    for suffix in _PULL_REQUEST_TEMPLATE_FILE_SUFFIXES
)


def _build_repository_contents_api_path(*, owner: str, name: str, path: str) -> str:
    base = f"repos/{owner}/{name}/contents"
    if not path:
        return base
    return f"{base}/{quote(path, safe='/')}"


def _build_repository_relative_path(*, parent_path: str, child_name: str) -> str:
    if not parent_path:
        return child_name
    return f"{parent_path}/{child_name}"


def _iter_direct_pull_request_template_candidate_paths() -> tuple[str, ...]:
    return tuple(
        _build_repository_relative_path(parent_path=parent_path, child_name=f"{basename}{suffix}")
        for parent_path in _PULL_REQUEST_TEMPLATE_PARENT_PATHS
        for basename in _DIRECT_PULL_REQUEST_TEMPLATE_BASENAME_VARIANTS
        for suffix in _PULL_REQUEST_TEMPLATE_FILE_SUFFIXES
    )


def _decode_repository_contents_text(payload: dict[str, object]) -> str | None:
    encoding = _as_optional_str(payload.get("encoding"))
    content = _as_optional_str(payload.get("content"))
    if encoding != "base64" or content is None:
        return None

    normalized = content.replace("\n", "")
    try:
        return base64.b64decode(normalized, validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def _is_direct_pull_request_template_name(name: str) -> bool:
    return name.casefold() in _DIRECT_PULL_REQUEST_TEMPLATE_FILENAMES


def _is_pull_request_template_directory_name(name: str) -> bool:
    return name.casefold() == "pull_request_template"


def _is_pull_request_template_path(path: str) -> bool:
    lowered = path.casefold()
    return lowered.endswith((".md", ".markdown", ".mdown", ".txt"))


def _is_gh_api_not_found_error(message: str) -> bool:
    lowered = message.casefold()
    return "404" in lowered and "not found" in lowered


def _clip_text(text: str | None, fallback: str, limit: int = MAX_INLINE_TEXT) -> tuple[str, bool]:
    if not text:
        return fallback, False
    trimmed = text.strip()
    if not trimmed:
        return fallback, False
    lines = [line.rstrip() for line in trimmed.splitlines()]
    normalized = "\n".join(lines)
    if len(normalized) <= limit and len(lines) <= MAX_INLINE_LINES:
        return normalized, False

    clipped = normalized[:limit].rstrip()
    clipped_lines = clipped.splitlines()[:MAX_INLINE_LINES]
    return "\n".join(clipped_lines).rstrip() + "...", True


def _first_non_empty_line(text: str | None) -> str | None:
    if text is None:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            return line
    return None


def _build_review_text(
    node: dict[str, object],
    ref: PullRequestRef,
    state: str,
    *,
    threads_for_review: list[dict[str, object]],
    timeline_window: TimelineWindow | None,
    show_resolved_details: bool,
    show_outdated_details: bool,
    show_minimized_details: bool,
    show_details_blocks: bool,
    review_threads_window: int | None,
    diff_hunk_lines: int | None,
    viewer_login: str,
) -> tuple[str, int, bool, int]:
    body = (_as_optional_str(node.get("body")) or "").strip()
    details_collapsed_count = 0
    if not show_details_blocks:
        body, body_details_count = _collapse_details_blocks(body)
        details_collapsed_count += body_details_count
    review_in_window = _timestamp_matches_timeline_window(
        _parse_datetime(_as_optional_str(node.get("submittedAt"))),
        timeline_window,
    )
    total_count = 0
    detail_lines: list[str] = []
    thread_blocks: list[list[str]] = []
    thread_visible_counts: list[int] = []
    resolved_hidden_count = 0
    rendered_comments = 0
    rendered_thread_index = 0
    has_clipped_diff_hunk = False
    for raw_thread in threads_for_review:
        thread = _as_dict(raw_thread, context="review thread")
        is_resolved = bool(thread.get("isResolved"))
        thread_id = _as_optional_str(thread.get("id")) or "(unknown thread id)"
        comment_nodes = _as_list(thread.get("comments"))
        matching_comment_count = _count_thread_comments_in_timeline_window(comment_nodes, timeline_window)
        thread_matches_window = matching_comment_count > 0
        if (
            timeline_window is not None
            and timeline_window.active
            and not review_in_window
            and not thread_matches_window
        ):
            continue
        total_count += len(comment_nodes)
        if is_resolved and not show_resolved_details and not thread_matches_window:
            resolved_hidden_count += len(comment_nodes)
            continue
        rendered_thread_index += 1
        thread_lines, visible_comments, thread_has_clipped_diff_hunk, thread_details_collapsed_count = (
            _render_review_thread_block(
                thread_id=thread_id,
                is_resolved=is_resolved,
                thread_index=rendered_thread_index,
                comments=comment_nodes,
                ref=ref,
                viewer_login=viewer_login,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                diff_hunk_lines=diff_hunk_lines,
                timeline_window=timeline_window,
                force_full_context=thread_matches_window,
                matching_comment_count=matching_comment_count,
            )
        )
        thread_blocks.append(thread_lines)
        thread_visible_counts.append(visible_comments)
        rendered_comments += visible_comments
        has_clipped_diff_hunk = has_clipped_diff_hunk or thread_has_clipped_diff_hunk
        details_collapsed_count += thread_details_collapsed_count

    shown_comments = rendered_comments
    if review_threads_window is not None and review_threads_window > 0 and len(thread_blocks) > review_threads_window:
        prefix_count = max(1, review_threads_window // 2)
        suffix_count = max(1, review_threads_window - prefix_count)
        hidden_threads = len(thread_blocks) - (prefix_count + suffix_count)
        shown_comments = sum(thread_visible_counts[:prefix_count]) + sum(thread_visible_counts[-suffix_count:])
        review_id = _as_optional_str(node.get("id")) or ""
        if review_id and hidden_threads > 0:
            hidden_start = prefix_count + 1
            hidden_end = prefix_count + hidden_threads
            hidden_label = str(hidden_start) if hidden_start == hidden_end else f"{hidden_start}-{hidden_end}"
            expand_cmd = display_command_with(
                f"pr review-expand {review_id} --pr {ref.number} --repo {ref.owner}/{ref.name}"
            )
            expand_hidden_cmd = display_command_with(
                f"pr review-expand {review_id} --threads {hidden_start}-{hidden_end} --pr {ref.number} --repo {ref.owner}/{ref.name}"
            )
            detail_lines.extend(_flatten_thread_blocks(thread_blocks[:prefix_count]))
            detail_lines.extend(
                [
                    "---",
                    f"Hidden conversations: {hidden_label} ({hidden_threads} hidden)",
                    f"⏎ run `{expand_hidden_cmd}` to load hidden conversations only.",
                    f"⏎ run `{expand_cmd}` to load all conversations.",
                    "---",
                ]
            )
            detail_lines.extend(_flatten_thread_blocks(thread_blocks[-suffix_count:]))
        else:
            detail_lines.extend(_flatten_thread_blocks(thread_blocks))
    else:
        detail_lines.extend(_flatten_thread_blocks(thread_blocks))

    chunks: list[str] = []
    if body:
        chunks.append(body)
    if total_count > 0:
        chunks.append(f"Review comments ({shown_comments}/{total_count} shown):")
        if detail_lines:
            chunks.extend(detail_lines)
    elif threads_for_review:
        chunks.append("Review comments (0/0 shown):")
        chunks.append("(review threads exist but contain no comment nodes)")

    if not chunks:
        return f"review state: {state.lower()}", resolved_hidden_count, has_clipped_diff_hunk, details_collapsed_count
    return "\n".join(chunks), resolved_hidden_count, has_clipped_diff_hunk, details_collapsed_count


def _render_review_thread_block(
    *,
    thread_id: str,
    is_resolved: bool,
    thread_index: int,
    comments: list[object],
    ref: PullRequestRef,
    viewer_login: str,
    show_outdated_details: bool,
    show_minimized_details: bool,
    show_details_blocks: bool,
    diff_hunk_lines: int | None,
    timeline_window: TimelineWindow | None = None,
    force_full_context: bool = False,
    matching_comment_count: int = 0,
) -> tuple[list[str], int, bool, int]:
    header = f"- Thread[{thread_index}] {thread_id}"
    if force_full_context and timeline_window is not None and timeline_window.active:
        noun = "update" if matching_comment_count == 1 else "updates"
        header += f" ({matching_comment_count} {noun} in selected window; full context shown)"
    lines = [header]
    visible_comments = 0
    minimized_hidden_count = 0
    minimized_reasons: set[str] = set()
    has_clipped_diff_hunk = False
    details_collapsed_count = 0
    for comment_index, raw_comment in enumerate(comments, start=1):
        comment = _as_dict(raw_comment, context="review comment")
        comment_id = _as_optional_str(comment.get("id")) or "(unknown comment id)"
        is_minimized = bool(comment.get("isMinimized"))
        if is_minimized and not show_minimized_details and not force_full_context:
            minimized_hidden_count += 1
            reasons: list[str] = []
            reason = _format_minimized_reason(comment.get("minimizedReason"))
            minimized_reasons.add(reason)
            reasons.append(reason)
            reason_text = ", ".join(sorted(set(reasons)))
            comment_expand_cmd = display_command_with(
                f"pr comment-expand {comment_id} --pr {ref.number} --repo {ref.owner}/{ref.name}"
            )
            lines.append(f"- [{comment_index}] (hidden comment: {reason_text})")
            lines.append(f"  ◌ comment_id: {comment_id}")
            lines.append(f"  ⏎ run `{comment_expand_cmd}`")
            continue
        comment_lines, comment_has_clipped_diff_hunk, comment_details_collapsed_count = _render_review_comment_block(
            comment=comment,
            index=comment_index,
            include_diff_hunk=(comment_index == 1),
            ref=ref,
            viewer_login=viewer_login,
            show_details_blocks=show_details_blocks,
            diff_hunk_lines=diff_hunk_lines,
            timeline_window=timeline_window,
        )
        lines.extend(comment_lines)
        visible_comments += 1
        has_clipped_diff_hunk = has_clipped_diff_hunk or comment_has_clipped_diff_hunk
        details_collapsed_count += comment_details_collapsed_count
    reply_cmd = display_command_with(
        f"pr thread-reply {thread_id} --body '<reply>' --pr {ref.number} --repo {ref.owner}/{ref.name}"
    )
    unresolve_cmd = display_command_with(
        f"pr thread-unresolve {thread_id} --pr {ref.number} --repo {ref.owner}/{ref.name}"
    )
    resolve_cmd = display_command_with(f"pr thread-resolve {thread_id} --pr {ref.number} --repo {ref.owner}/{ref.name}")
    lines.append(f"  ◌ thread_id: {thread_id}")
    lines.append("  ⌨ reply_body: '<reply>'")
    lines.append("  ⌨ reply_body_file: '<reply.md>'")
    lines.append(f"  ⏎ Reply via {display_command()}: `{reply_cmd}`")
    lines.append(
        "  ⏎ Multi-line reply via "
        + f"{display_command()}: `{display_command_with(f'pr thread-reply {thread_id} --body-file <reply.md> --pr {ref.number} --repo {ref.owner}/{ref.name}')}`"
    )
    if is_resolved:
        lines.append(f"  ⏎ Unresolve via {display_command()}: `{unresolve_cmd}`")
    else:
        lines.append(f"  ⏎ Resolve via {display_command()}: `{resolve_cmd}`")
    return lines, visible_comments, has_clipped_diff_hunk, details_collapsed_count


def _build_review_minimized_summary(
    *,
    threads_for_review: list[dict[str, object]],
    show_resolved_details: bool,
    show_outdated_details: bool,
    show_minimized_details: bool,
) -> tuple[int, str | None]:
    if show_minimized_details:
        return 0, None
    reasons: set[str] = set()
    count = 0
    for raw_thread in threads_for_review:
        thread = _as_dict(raw_thread, context="review thread for minimized summary")
        if bool(thread.get("isResolved")) and not show_resolved_details:
            continue
        for raw_comment in _as_list(thread.get("comments")):
            comment = _as_dict(raw_comment, context="review comment for minimized summary")
            is_minimized = bool(comment.get("isMinimized"))
            if not (is_minimized and not show_minimized_details):
                continue
            count += 1
            reasons.add(_format_minimized_reason(comment.get("minimizedReason")))
    if count == 0:
        return 0, None
    return count, ", ".join(sorted(reasons))


def _flatten_thread_blocks(blocks: list[list[str]]) -> list[str]:
    merged: list[str] = []
    for block in blocks:
        merged.extend(block)
    return merged


def _collect_review_comment_timestamps(threads_for_review: list[dict[str, object]]) -> tuple[datetime, ...]:
    timestamps: list[datetime] = []
    for raw_thread in threads_for_review:
        thread = _as_dict(raw_thread, context="review thread timestamps")
        timestamps.extend(_collect_thread_comment_timestamps(_as_list(thread.get("comments"))))
    return tuple(timestamps)


def _collect_thread_comment_timestamps(comments: list[object]) -> list[datetime]:
    timestamps: list[datetime] = []
    for raw_comment in comments:
        comment = _as_dict(raw_comment, context="review comment timestamp")
        created_at = _as_optional_str(comment.get("createdAt"))
        if created_at is None:
            continue
        timestamps.append(_parse_datetime(created_at))
    return timestamps


def _count_thread_comments_in_timeline_window(
    comments: list[object],
    timeline_window: TimelineWindow | None,
) -> int:
    if timeline_window is None or not timeline_window.active:
        return 0
    return sum(
        1
        for timestamp in _collect_thread_comment_timestamps(comments)
        if _timestamp_matches_timeline_window(timestamp, timeline_window)
    )


def _timestamp_matches_timeline_window(timestamp: datetime, timeline_window: TimelineWindow | None) -> bool:
    if timeline_window is None or not timeline_window.active:
        return False
    if timeline_window.after is not None and timestamp <= timeline_window.after:
        return False
    if timeline_window.before is not None and timestamp >= timeline_window.before:
        return False
    return True


def _format_timeline_window_comment_marker(
    timestamp: datetime | None,
    timeline_window: TimelineWindow | None,
) -> str:
    if timestamp is None or timeline_window is None or not timeline_window.active:
        return ""
    if _timestamp_matches_timeline_window(timestamp, timeline_window):
        return " [within selected window]"
    if timeline_window.after is not None and timestamp <= timeline_window.after:
        return " [before selected window]"
    if timeline_window.before is not None and timestamp >= timeline_window.before:
        return " [after selected window]"
    return ""


def _format_minimized_reason(value: object) -> str:
    raw = (_as_optional_str(value) or "unknown").strip()
    if not raw:
        return "unknown"
    return raw.lower().replace("_", " ")


def _render_review_comment_block(
    comment: dict[str, object],
    index: int,
    *,
    include_diff_hunk: bool = True,
    ref: PullRequestRef,
    viewer_login: str,
    show_details_blocks: bool,
    diff_hunk_lines: int | None,
    timeline_window: TimelineWindow | None = None,
) -> tuple[list[str], bool, int]:
    path = _as_optional_str(comment.get("path")) or "(unknown path)"
    line = _as_line_ref(comment)
    author = _get_actor_display(comment.get("author"))
    created_at_raw = _as_optional_str(comment.get("createdAt"))
    created_at = created_at_raw or "unknown time"
    created_at_value = _parse_datetime(created_at_raw) if created_at_raw is not None else None
    body = (_as_optional_str(comment.get("body")) or "").strip()
    diff_hunk = (_as_optional_str(comment.get("diffHunk")) or "").strip()
    suggestion_lines = _extract_suggestion_lines(body)
    rendered_body = _strip_suggestion_blocks(body).strip()
    if not rendered_body and not suggestion_lines:
        rendered_body = body
    details_collapsed_count = 0
    if rendered_body and not show_details_blocks:
        rendered_body, details_collapsed_count = _collapse_details_blocks(rendered_body)

    outdated_badge = " [outdated]" if (bool(comment.get("outdated")) or bool(comment.get("isOutdated"))) else ""
    window_marker = _format_timeline_window_comment_marker(created_at_value, timeline_window)
    lines = [f"- [{index}]{outdated_badge} {path}{line} by @{author} at {created_at}{window_marker}"]
    if rendered_body:
        lines.append("  Comment:")
        lines.extend(_indented_tag_block("comment", rendered_body, indent="  "))
    has_clipped_diff_hunk = False
    if diff_hunk and include_diff_hunk:
        rendered_diff_hunk = diff_hunk
        shown_lines = len(diff_hunk.splitlines())
        total_lines = shown_lines
        if diff_hunk_lines is not None and diff_hunk_lines > 0:
            rendered_diff_hunk, has_clipped_diff_hunk, shown_lines, total_lines = _clip_diff_hunk_lines(
                diff_hunk=diff_hunk,
                max_lines=diff_hunk_lines,
            )
        lines.append("  Diff Hunk:")
        lines.extend(_indented_fenced_block("diff", rendered_diff_hunk, indent="  "))
        if has_clipped_diff_hunk:
            lines.append(f"  ... diff hunk clipped ({shown_lines}/{total_lines} lines).")

    suggestion_diff = _suggestion_to_diff(path=path, line_ref=line, body=body)
    if suggestion_diff:
        lines.append("  Suggested Change:")
        lines.extend(_indented_fenced_block("diff", suggestion_diff, indent="  "))
    reactions_summary = _format_reactions(comment.get("reactionGroups"))
    if reactions_summary:
        lines.append(f"  Reactions: {reactions_summary}")
    comment_id = _as_optional_str(comment.get("id")) or ""
    if comment_id and author == viewer_login:
        edit_cmd = display_command_with(
            f"pr comment-edit {comment_id} --body '<comment_body>' --pr {ref.number} --repo {ref.owner}/{ref.name}"
        )
        edit_file_cmd = display_command_with(
            f"pr comment-edit {comment_id} --body-file <comment.md> --pr {ref.number} --repo {ref.owner}/{ref.name}"
        )
        lines.append(f"  ◌ comment_id: {comment_id}")
        lines.append("  ⌨ comment_body: '<comment_body>'")
        lines.append("  ⌨ comment_body_file: '<comment.md>'")
        lines.append(f"  ⏎ Edit comment via {display_command()}: `{edit_cmd}`")
        lines.append(f"  ⏎ Multi-line edit via {display_command()}: `{edit_file_cmd}`")

    if not body and not diff_hunk:
        lines.append("  (empty review comment)")
    return lines, has_clipped_diff_hunk, details_collapsed_count


def _clip_diff_hunk_lines(diff_hunk: str, max_lines: int) -> tuple[str, bool, int, int]:
    lines = diff_hunk.splitlines()
    total = len(lines)
    if total <= max_lines or max_lines <= 0:
        return diff_hunk, False, total, total

    head = max_lines // 2
    tail = max_lines - head
    if head <= 0:
        head = 1
        tail = max_lines - 1
    if tail <= 0:
        tail = 1
        head = max_lines - 1
    clipped = [*lines[:head], f"... ({total - max_lines} lines omitted) ...", *lines[-tail:]]
    return "\n".join(clipped), True, max_lines, total


def _as_line_ref(comment: dict[str, object]) -> str:
    line = _as_int_default(comment.get("line"), default=0)
    original_line = _as_int_default(comment.get("originalLine"), default=0)
    start_line = _as_int_default(comment.get("startLine"), default=0)
    original_start_line = _as_int_default(comment.get("originalStartLine"), default=0)

    if line > 0:
        return f":L{line}"
    if original_line > 0:
        return f":L{original_line}"
    if start_line > 0:
        return f":L{start_line}"
    if original_start_line > 0:
        return f":L{original_start_line}"
    return ""


def _indented_fenced_block(language: str, content: str, indent: str = "") -> list[str]:
    out = [f"{indent}```{language}"]
    out.extend(f"{indent}{line}" for line in content.splitlines())
    out.append(f"{indent}```")
    return out


def _indented_tag_block(tag: str, content: str, indent: str = "") -> list[str]:
    out = [f"{indent}<{tag}>"]
    out.extend(f"{indent}{line}" for line in content.splitlines())
    out.append(f"{indent}</{tag}>")
    return out


def _suggestion_to_diff(path: str, line_ref: str, body: str) -> str | None:
    suggestion_lines = _extract_suggestion_lines(body)
    if not suggestion_lines:
        return None
    header = f"@@ {path}{line_ref} @@"
    plus_lines = [f"+{line}" for line in suggestion_lines]
    return "\n".join([header, *plus_lines])


def _extract_suggestion_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = -1
    end = -1
    for idx, line in enumerate(lines):
        if line.strip().startswith("```suggestion"):
            start = idx + 1
            continue
        if start >= 0 and line.strip().startswith("```"):
            end = idx
            break
    if start < 0:
        return []
    if end < 0:
        end = len(lines)
    return [line.rstrip() for line in lines[start:end]]


def _strip_suggestion_blocks(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_suggestion = False
    for line in lines:
        marker = line.strip().lower()
        if not in_suggestion and marker.startswith("```suggestion"):
            in_suggestion = True
            continue
        if in_suggestion and marker.startswith("```"):
            in_suggestion = False
            continue
        if not in_suggestion:
            out.append(line.rstrip())
    return "\n".join(out)


def _collapse_details_blocks(text: str | None) -> tuple[str, int]:
    if not text:
        return "", 0
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        block = match.group(1) or ""
        summary_match = SUMMARY_RE.search(block)
        summary = _strip_html_tags(summary_match.group(1) if summary_match else "") or "details"
        return f"\n<details>\n<summary>{summary}</summary>\n(details body collapsed)\n</details>\n"

    collapsed = DETAILS_BLOCK_RE.sub(repl, text)
    return collapsed, count


def _strip_html_tags(text: str) -> str:
    no_tags = HTML_TAG_RE.sub("", text or "")
    return " ".join(no_tags.split())


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _as_dict(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {context} structure")
    raw = cast("dict[object, object]", value)
    return {str(k): v for k, v in raw.items()}


def _as_dict_optional(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        raw = cast("dict[object, object]", value)
        return {str(k): v for k, v in raw.items()}
    return None


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast("list[object]", value)
    return []


def _count_approved_reviewers(reviews_obj: dict[str, object] | None) -> int | None:
    if reviews_obj is None:
        return None
    nodes = _as_list(reviews_obj.get("nodes"))
    approved_by: set[str] = set()
    for raw in nodes:
        review = _as_dict(raw, context="review")
        if (_as_optional_str(review.get("state")) or "") != "APPROVED":
            continue
        author = _get_login(review.get("author"))
        if author and author != "unknown":
            approved_by.add(author)
    return len(approved_by)


def _extract_co_author_trailers(payload: dict[str, object]) -> list[str]:
    commits_obj = _as_dict_optional(payload.get("commits"))
    if commits_obj is None:
        return []
    trailers: list[str] = []
    seen: set[str] = set()
    for raw in _as_list(commits_obj.get("nodes")):
        commit = _as_dict_optional(raw)
        if commit is None:
            continue
        text_parts = [
            _as_optional_str(commit.get("messageHeadline")) or "",
            _as_optional_str(commit.get("messageBody")) or "",
        ]
        merged_text = "\n".join(part for part in text_parts if part)
        for match in CO_AUTHORED_BY_RE.finditer(merged_text):
            name = " ".join(match.group(1).split())
            email = " ".join(match.group(2).split())
            if not name or not email:
                continue
            trailer = f"Co-authored-by: {name} <{email}>"
            normalized = trailer.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            trailers.append(trailer)
    return trailers


def _extract_label_names(payload: dict[str, object]) -> list[str]:
    out: list[str] = []
    for label in _as_list(payload.get("labels")):
        label_dict = _as_dict_optional(label)
        if label_dict is None:
            continue
        name = _as_optional_str(label_dict.get("name"))
        if name:
            out.append(name)
    return out


def _normalized_optional_str(value: object) -> str | None:
    raw = _as_optional_str(value)
    if raw is None:
        return None
    normalized = raw.strip()
    return normalized or None


def _extract_parent_repo_full_name(parent_obj: dict[str, object] | None) -> str | None:
    if parent_obj is None:
        return None

    direct = _normalized_optional_str(parent_obj.get("nameWithOwner"))
    if direct is not None:
        return direct

    full_name = _normalized_optional_str(parent_obj.get("full_name"))
    if full_name is not None:
        return full_name

    owner_obj = _as_dict_optional(parent_obj.get("owner"))
    owner_login = _normalized_optional_str(owner_obj.get("login")) if owner_obj is not None else None
    name = _normalized_optional_str(parent_obj.get("name"))
    if owner_login is not None and name is not None:
        return f"{owner_login}/{name}"
    return None


def _collect_repo_documents(tree_items: list[object], *, kind: str) -> tuple[RepoDocument, ...]:
    docs: list[RepoDocument] = []
    seen: set[str] = set()
    for raw_item in tree_items:
        item = _as_dict_optional(raw_item)
        if item is None:
            continue
        if (_as_optional_str(item.get("type")) or "") != "blob":
            continue
        path = _normalized_optional_str(item.get("path"))
        if path is None or not _matches_repo_document_kind(path, kind=kind):
            continue
        normalized = path.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        docs.append(RepoDocument(path=path))
    docs.sort(key=lambda item: item.path.lower())
    return tuple(docs)


def _merge_repo_tree_items(primary: list[object], extras: list[object]) -> list[object]:
    merged: list[object] = []
    seen: set[tuple[str, str]] = set()
    for raw_item in [*primary, *extras]:
        item = _as_dict_optional(raw_item)
        if item is None:
            continue
        path = _normalized_optional_str(item.get("path"))
        item_type = _normalized_optional_str(item.get("type"))
        if path is None or item_type is None:
            continue
        key = (path.lower(), item_type.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append({"path": path, "type": item_type})
    return merged


def _matches_repo_document_kind(path: str, *, kind: str) -> bool:
    normalized = path.lower()
    base_name = normalized.rsplit("/", 1)[-1]
    if kind == "contributing":
        return base_name.startswith("contributing")
    if kind == "agents":
        return base_name == "agents.md"
    if kind == "codeowners":
        return base_name == "codeowners"
    if kind == "pr_template":
        if base_name == "pull_request_template" or base_name.startswith("pull_request_template."):
            return True
        return "/pull_request_template/" in normalized
    raise RuntimeError(f"unknown repository document kind: {kind}")


def _extract_required_status_check_contexts(required_status_checks_obj: dict[str, object] | None) -> tuple[str, ...]:
    if required_status_checks_obj is None:
        return ()

    contexts: list[str] = []
    seen: set[str] = set()

    def add_context(value: object) -> None:
        context = _normalized_optional_str(value)
        if context is None:
            return
        lowered = context.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        contexts.append(context)

    for raw_check in _as_list(required_status_checks_obj.get("checks")):
        check = _as_dict_optional(raw_check)
        if check is None:
            continue
        add_context(check.get("context"))

    if not contexts:
        for raw_context in _as_list(required_status_checks_obj.get("contexts")):
            add_context(raw_context)

    return tuple(contexts)


def _build_branch_protection_rule_candidate(
    *,
    default_branch: str,
    raw_rule: object,
) -> tuple[tuple[int, int, int], RepoBranchProtection] | None:
    rule = _as_dict_optional(raw_rule)
    if rule is None:
        return None
    pattern = _normalized_optional_str(rule.get("pattern"))
    if pattern is None or not fnmatchcase(default_branch, pattern):
        return None
    contexts = tuple(
        context
        for raw_context in _as_list(rule.get("requiredStatusCheckContexts"))
        if (context := _normalized_optional_str(raw_context)) is not None
    )
    summary = RepoBranchProtection(
        pattern=pattern,
        source="graphql",
        requires_status_checks=bool(rule.get("requiresStatusChecks")),
        required_status_check_contexts=contexts,
        requires_approving_reviews=(
            None if rule.get("requiresApprovingReviews") is None else bool(rule.get("requiresApprovingReviews"))
        ),
        required_approving_review_count=(
            None
            if rule.get("requiredApprovingReviewCount") is None
            else _as_int_default(rule.get("requiredApprovingReviewCount"), default=0)
        ),
        requires_code_owner_reviews=(
            None if rule.get("requiresCodeOwnerReviews") is None else bool(rule.get("requiresCodeOwnerReviews"))
        ),
        is_admin_enforced=(None if rule.get("isAdminEnforced") is None else bool(rule.get("isAdminEnforced"))),
    )
    return (_branch_protection_specificity(pattern, default_branch), summary)


def _branch_protection_specificity(pattern: str, default_branch: str) -> tuple[int, int, int]:
    is_exact = 0 if pattern == default_branch else 1
    wildcard_count = pattern.count("*") + pattern.count("?")
    return (is_exact, wildcard_count, -len(pattern))


def _parse_conflict_files_from_git_output(output: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for match in GIT_CONFLICT_FILE_RE.finditer(output):
        path = match.group(1).strip()
        if not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _parse_merge_tree_name_only_output(output: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("warning:") or line.startswith("error:"):
            continue
        if line in seen:
            continue
        seen.add(line)
        files.append(line)
    return files


def _run_plain_command(cmd: list[str], *, cwd: str) -> bool:
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.returncode == 0


def _as_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: object, *, context: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as error:
            raise RuntimeError(f"invalid integer for {context}: {value}") from error
    raise RuntimeError(f"invalid integer value for {context}")


def _as_int_default(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _has_graphql_errors(payload: dict[str, object]) -> bool:
    return len(_as_list(payload.get("errors"))) > 0


def _format_reactions(value: object) -> str | None:
    groups = _as_list(value)
    parts: list[str] = []
    for raw_group in groups:
        group = _as_dict_optional(raw_group)
        if group is None:
            continue
        users_obj = _as_dict_optional(group.get("users"))
        if users_obj is None:
            continue
        count = _as_int_default(users_obj.get("totalCount"), default=0)
        if count <= 0:
            continue
        content = _as_optional_str(group.get("content")) or ""
        emoji = _reaction_emoji(content)
        if emoji:
            parts.append(f"{emoji} x{count}")
    if not parts:
        return None
    return " ".join(parts)


def _reaction_emoji(content: str) -> str:
    mapping = {
        "THUMBS_UP": "👍",
        "THUMBS_DOWN": "👎",
        "LAUGH": "😄",
        "HOORAY": "🎉",
        "CONFUSED": "😕",
        "HEART": "❤️",
        "ROCKET": "🚀",
        "EYES": "👀",
    }
    return mapping.get(content, "")


def _is_retryable_gh_error(stderr: str) -> bool:
    lowered = stderr.lower()
    retryable_patterns = (
        'post "https://api.github.com/graphql": eof',
        "eof",
        "timeout",
        "tls handshake timeout",
        "connection reset",
        "connection refused",
        "temporary failure",
    )
    return any(pattern in lowered for pattern in retryable_patterns)


def _is_check_run_passed(*, status: str, conclusion: str | None) -> bool:
    if status != "COMPLETED":
        return False
    return (conclusion or "").upper() in {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _extract_actions_run_and_job_ids(details_url: str | None) -> tuple[int | None, int | None]:
    if not details_url:
        return None, None
    parsed = urlparse(details_url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    # Expected shape: /<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
    run_id: int | None = None
    job_id: int | None = None
    for idx, part in enumerate(parts):
        if part == "runs" and idx + 1 < len(parts):
            run_id = _parse_positive_int(parts[idx + 1])
        if part == "job" and idx + 1 < len(parts):
            job_id = _parse_positive_int(parts[idx + 1])
    return run_id, job_id


def _parse_positive_int(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _reference_subject_summary(source: dict[str, object] | None) -> ReferenceSubject | None:
    if source is None:
        return None
    source_type = _as_optional_str(source.get("__typename")) or ""
    source_number = _as_int_default(source.get("number"), default=0)
    if source_number <= 0:
        return None
    repo_obj = _as_dict_optional(source.get("repository"))
    source_repo = _as_optional_str(repo_obj.get("nameWithOwner")) if repo_obj is not None else None
    if not source_repo:
        return None
    title = (_as_optional_str(source.get("title")) or "").strip()
    author = _get_actor_display(source.get("author"))
    detail_parts: list[str] = []
    if title:
        detail_parts.append(f'"{title}"')
    if author != "unknown":
        detail_parts.append(f"by @{author}")
    detail = " ".join(detail_parts).strip()
    return ReferenceSubject(type=source_type, number=source_number, repo=source_repo, detail=detail)
