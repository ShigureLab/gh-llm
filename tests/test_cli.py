from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from typing import TYPE_CHECKING, Any

from gh_llm import __version__, cli, github_api
from gh_llm.commands import doctor as doctor_commands, pr as pr_commands
from gh_llm.models import ReviewThreadSummary

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class GhResponder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pending_review_id: str | None = None

    def run(self, cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        del check, capture_output, text
        self.calls.append(cmd)

        if cmd[:3] == ["gh", "pr", "view"]:
            pr_number = self._extract_pr_number(cmd)
            state = "OPEN"
            changed_files = 1
            if pr_number == 77827:
                state = "CLOSED"
            elif pr_number == 77960:
                state = "MERGED"
            elif pr_number == 78255:
                changed_files = 3
            merge_state_status = "CLEAN"
            mergeable = "MERGEABLE"
            if pr_number == 77971:
                merge_state_status = "DIRTY"
                mergeable = "CONFLICTING"
            commits_payload: dict[str, Any] = {"nodes": []}
            if pr_number == 77972:
                commits_payload = {
                    "nodes": [
                        {
                            "messageHeadline": "Feature change",
                            "messageBody": "Improvements\n\nCo-authored-by: Alice Example <alice@example.com>",
                        },
                        {
                            "messageHeadline": "Follow-up",
                            "messageBody": "Co-authored-by: Bob Example <bob@example.com>",
                        },
                    ]
                }
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "number": pr_number,
                        "title": "Timeline test",
                        "url": f"https://github.com/PaddlePaddle/Paddle/pull/{pr_number}",
                        "author": {"login": "ShigureNyako"},
                        "state": state,
                        "isDraft": False,
                        "body": "This is PR description",
                        "updatedAt": "2026-02-16T09:00:00Z",
                        "changedFiles": changed_files,
                        "mergeStateStatus": merge_state_status,
                        "mergeable": mergeable,
                        "commits": commits_payload,
                        "reactionGroups": [{"content": "ROCKET", "users": {"totalCount": 1}}],
                    }
                )
            )

        if cmd[:3] == ["gh", "pr", "diff"]:
            return FakeCompletedProcess(
                "\n".join(
                    [
                        "diff --git a/python/test_file.py b/python/test_file.py",
                        "index 1111111..2222222 100644",
                        "--- a/python/test_file.py",
                        "+++ b/python/test_file.py",
                        "@@ -20,2 +20,2 @@ def demo():",
                        "-old_api_call()",
                        "+new_api_call()",
                    ]
                )
                + "\n"
            )

        if cmd[:3] == ["gh", "issue", "view"]:
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "number": 77924,
                        "title": "Issue timeline test",
                        "url": "https://github.com/PaddlePaddle/Paddle/issues/77924",
                        "author": {"login": "ShigureNyako"},
                        "state": "OPEN",
                        "body": "This is issue description",
                        "updatedAt": "2026-02-16T09:00:00Z",
                        "reactionGroups": [{"content": "EYES", "users": {"totalCount": 1}}],
                    }
                )
            )

        if cmd[:3] == ["gh", "repo", "view"]:
            repo = cmd[3] if len(cmd) > 3 else "PaddlePaddle/Paddle"
            return FakeCompletedProcess(json.dumps(_repo_view_payload(repo)))

        if cmd[:3] == ["gh", "api", "user"]:
            return FakeCompletedProcess(json.dumps({"login": "ShigureNyako"}))

        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/pulls/" in cmd[2] and "/files?" in cmd[2]:
            return FakeCompletedProcess(json.dumps(_pull_files_payload(cmd[2])))

        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/git/trees/" in cmd[2]:
            return FakeCompletedProcess(json.dumps(_repo_tree_payload(cmd[2])))

        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/branches/" in cmd[2]:
            payload = _repo_branch_payload(cmd[2])
            if payload is None:
                return FakeCompletedProcess("", returncode=1, stderr="HTTP 404: Not Found")
            return FakeCompletedProcess(json.dumps(payload))

        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and cmd[2].startswith("repos/") and "/contents" not in cmd[2]:
            payload = _repository_api_payload(cmd[2])
            if payload is None:
                return FakeCompletedProcess("", returncode=1, stderr="HTTP 404: Not Found")
            return FakeCompletedProcess(json.dumps(payload))

        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/contents" in cmd[2]:
            payload = _repository_contents_payload(cmd[2])
            if payload is None:
                return FakeCompletedProcess("", returncode=1, stderr="HTTP 404: Not Found")
            return FakeCompletedProcess(json.dumps(payload))

        if cmd[:3] != ["gh", "api", "graphql"]:
            return FakeCompletedProcess("", returncode=1, stderr="unexpected command")

        query = _extract_form(cmd, "query")
        first = _extract_field_int(cmd, "pageSize")
        after = _extract_field(cmd, "after")
        before = _extract_field(cmd, "before")

        if "branchProtectionRules(first:100" in query:
            return FakeCompletedProcess(json.dumps(_branch_protection_rules_payload(after=after)))

        if "reviewThreads(first:100" in query:
            payload = _review_threads_payload(after=after)
            return FakeCompletedProcess(json.dumps(payload))

        if "statusCheckRollup" in query:
            return FakeCompletedProcess(json.dumps(_checks_payload()))

        if "addPullRequestReviewThreadReply" in query:
            return FakeCompletedProcess(
                json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "PRRC_reply_1"}}}})
            )

        if "addPullRequestReviewThread" in query:
            self.pending_review_id = "PRR_pending_1"
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "data": {
                            "addPullRequestReviewThread": {
                                "thread": {
                                    "id": "PRRT_new_1",
                                    "comments": {"nodes": [{"id": "PRRC_new_1"}]},
                                }
                            }
                        }
                    }
                )
            )

        if "addPullRequestReview(input:" in query:
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "data": {
                            "addPullRequestReview": {
                                "pullRequestReview": {
                                    "id": "PRR_new_1",
                                    "state": "PENDING",
                                }
                            }
                        }
                    }
                )
            )

        if "reviews(last:50)" in query:
            nodes: list[dict[str, Any]] = []
            if self.pending_review_id is not None:
                nodes.append(
                    {
                        "id": self.pending_review_id,
                        "state": "PENDING",
                        "author": {"login": "ShigureNyako"},
                    }
                )
            return FakeCompletedProcess(
                json.dumps({"data": {"repository": {"pullRequest": {"reviews": {"nodes": nodes}}}}})
            )

        if "submitPullRequestReview(input:" in query:
            review_id = self.pending_review_id or "PRR_pending_1"
            self.pending_review_id = None
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "data": {
                            "submitPullRequestReview": {
                                "pullRequestReview": {
                                    "id": review_id,
                                    "state": "COMMENTED",
                                }
                            }
                        }
                    }
                )
            )

        if "pullRequest(number:$number){" in query and "id" in query and "timelineItems" not in query:
            if "headRefName" in query and "headRepository" in query:
                pr_number = _extract_field_int(cmd, "number")
                repo_payload: dict[str, Any] = {
                    "mergeCommitAllowed": True,
                    "squashMergeAllowed": True,
                    "rebaseMergeAllowed": True,
                }
                if pr_number == 77827:
                    return FakeCompletedProcess(
                        json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        **repo_payload,
                                        "pullRequest": {
                                            "id": "PR_kwDOA-qtos5closed",
                                            "merged": False,
                                            "headRefName": "feature/keep-branch",
                                            "headRefOid": "1111111111111111111111111111111111111111",
                                            "headRepository": {"nameWithOwner": "PaddlePaddle/Paddle"},
                                        },
                                    }
                                }
                            }
                        )
                    )
                if pr_number == 77960:
                    return FakeCompletedProcess(
                        json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        **repo_payload,
                                        "pullRequest": {
                                            "id": "PR_kwDOA-qtos5merged",
                                            "merged": True,
                                            "headRefName": "feature/deleted-branch",
                                            "headRefOid": "2222222222222222222222222222222222222222",
                                            "headRepository": {"nameWithOwner": "PaddlePaddle/Paddle"},
                                        },
                                    }
                                }
                            }
                        )
                    )
                if pr_number == 77972:
                    repo_payload["rebaseMergeAllowed"] = False
                return FakeCompletedProcess(
                    json.dumps(
                        {
                            "data": {
                                "repository": {
                                    **repo_payload,
                                    "pullRequest": {
                                        "id": "PR_kwDOA-qtos5xxxx",
                                        "merged": False,
                                        "headRefName": "feature/open-branch",
                                        "headRefOid": "3333333333333333333333333333333333333333",
                                        "headRepository": {"nameWithOwner": "PaddlePaddle/Paddle"},
                                    },
                                }
                            }
                        }
                    )
                )
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "id": "PR_kwDOA-qtos5xxxx",
                                }
                            }
                        }
                    }
                )
            )

        if "node(id:$id)" in query and "PullRequestReviewComment" in query:
            comment_id = _extract_field(cmd, "id")
            if comment_id == "PRRC_self_1":
                return FakeCompletedProcess(
                    json.dumps(
                        {
                            "data": {
                                "node": {
                                    "__typename": "PullRequestReviewComment",
                                    "id": "PRRC_self_1",
                                    "createdAt": "2026-02-14T14:50:03Z",
                                    "body": "self reply",
                                    "outdated": True,
                                    "isMinimized": False,
                                    "minimizedReason": None,
                                    "path": "python/test_file.py",
                                    "line": 23,
                                    "originalLine": 23,
                                    "diffHunk": "@@ -23,1 +23,1 @@\n-old\n+new",
                                    "author": {"login": "ShigureNyako"},
                                    "reactionGroups": [],
                                    "pullRequestReview": {"id": "PRR_mock"},
                                }
                            }
                        }
                    )
                )
            if comment_id == "c1":
                return FakeCompletedProcess(
                    json.dumps(
                        {
                            "data": {
                                "node": {
                                    "__typename": "IssueComment",
                                    "id": "c1",
                                    "createdAt": "2026-02-14T14:31:36Z",
                                    "body": ("LONG_TEXT " * 220) + "END_MARKER",
                                    "isMinimized": False,
                                    "minimizedReason": None,
                                    "author": {"login": "bot"},
                                    "reactionGroups": [{"content": "THUMBS_UP", "users": {"totalCount": 2}}],
                                }
                            }
                        }
                    )
                )
            if comment_id == "ic1":
                return FakeCompletedProcess(
                    json.dumps(
                        {
                            "data": {
                                "node": {
                                    "__typename": "IssueComment",
                                    "id": "ic1",
                                    "createdAt": "2026-02-13T10:00:00Z",
                                    "body": "ISSUE LONG BODY",
                                    "isMinimized": True,
                                    "minimizedReason": "OUTDATED",
                                    "author": {"login": "bot"},
                                    "reactionGroups": [{"content": "THUMBS_UP", "users": {"totalCount": 1}}],
                                }
                            }
                        }
                    )
                )
            return FakeCompletedProcess(json.dumps({"data": {"node": None}}))

        if "ref(qualifiedName:$qualifiedName)" in query:
            qualified = _extract_field(cmd, "qualifiedName")
            if qualified == "refs/heads/feature/deleted-branch":
                return FakeCompletedProcess(json.dumps({"data": {"repository": {"ref": None}}}))
            return FakeCompletedProcess(json.dumps({"data": {"repository": {"ref": {"id": "REF_kwDOA-qtos5yyyy"}}}}))

        if "updatePullRequestReviewComment" in query:
            return FakeCompletedProcess(
                json.dumps(
                    {"data": {"updatePullRequestReviewComment": {"pullRequestReviewComment": {"id": "PRRC_self_1"}}}}
                )
            )

        if "updateIssueComment" in query:
            return FakeCompletedProcess(json.dumps({"data": {"updateIssueComment": {"issueComment": {"id": "c3"}}}}))

        if "unresolveReviewThread" in query:
            return FakeCompletedProcess(
                json.dumps({"data": {"unresolveReviewThread": {"thread": {"id": "PRRT_mock_2", "isResolved": False}}}})
            )

        if "resolveReviewThread" in query:
            return FakeCompletedProcess(
                json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "PRRT_mock_1", "isResolved": True}}}})
            )

        if "timelineItems(first:" in query:
            if "issue(number:$number)" in query:
                payload = _issue_forward_page_payload(page_size=first, after=after)
                return FakeCompletedProcess(json.dumps(payload))
            payload = _forward_page_payload(page_size=first, after=after)
            return FakeCompletedProcess(json.dumps(payload))

        if "issue(number:$number)" in query:
            payload = _issue_backward_page_payload(page_size=first, before=before)
            return FakeCompletedProcess(json.dumps(payload))
        payload = _backward_page_payload(page_size=first, before=before)
        return FakeCompletedProcess(json.dumps(payload))

    @staticmethod
    def _extract_pr_number(cmd: list[str]) -> int:
        for token in cmd[3:]:
            if token.startswith("-"):
                continue
            if token.isdigit():
                return int(token)
        return 77928


def test_doctor_reports_entrypoint_probes_and_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entrypoint_version = __version__
    gh_version = "gh version test-build"

    def fake_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        del check, capture_output, text
        if cmd == ["gh", "llm", "--version"]:
            return FakeCompletedProcess(f"{entrypoint_version}\n")
        if cmd == ["gh", "--version"]:
            return FakeCompletedProcess(f"{gh_version}\nhttps://github.com/cli/cli/releases/tag/test-build\n")
        if cmd == ["gh", "auth", "status", "--active", "--hostname", "github.com"]:
            return FakeCompletedProcess("github.com\n  ✓ Logged in to github.com account ShigureNyako\n")
        if cmd == ["gh", "api", "user"]:
            return FakeCompletedProcess(json.dumps({"login": "ShigureNyako"}))
        if cmd[:3] == ["gh", "api", "graphql"]:
            return FakeCompletedProcess(json.dumps({"data": {"viewer": {"login": "ShigureNyako"}}}))
        return FakeCompletedProcess("", returncode=1, stderr="unexpected command")

    def fake_which(name: str) -> str | None:
        mapping = {
            "gh": "/opt/homebrew/bin/gh",
            "gh-llm": "/Users/test/bin/gh-llm",
        }
        return mapping.get(name)

    monkeypatch.setattr(doctor_commands.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor_commands.shutil, "which", fake_which)
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")
    monkeypatch.setenv("https_proxy", "http://proxy.example.test:8443")
    monkeypatch.setenv("all_proxy", "socks5://proxy.example.test:1080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setattr(sys, "argv", ["gh-llm"])

    code = cli.run(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "- entrypoint: gh llm" in out
    assert "- entrypoint_path: /opt/homebrew/bin/gh" in out
    assert "- gh_llm_path: /Users/test/bin/gh-llm" in out
    assert "- target_host: github.com" in out
    assert "- https_proxy: http://proxy.example.test:8443" in out
    assert "- all_proxy: socks5://proxy.example.test:1080" in out
    assert "- no_proxy: localhost,127.0.0.1" in out
    assert "- GH_TOKEN: (set)" in out
    assert f"- entrypoint version (`gh llm --version`): {entrypoint_version}" in out
    assert "- auth status (`gh auth status --active --hostname github.com`): ok" in out
    assert "- REST user probe (`gh api user`): ok (@ShigureNyako)" in out
    assert "- GraphQL viewer probe (`gh api graphql -f query='query{viewer{login}}'`): ok (@ShigureNyako)" in out
    assert "status: ok" in out


def test_doctor_scopes_auth_status_to_target_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    entrypoint_version = __version__

    def fake_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        del check, capture_output, text
        calls.append(cmd)
        if cmd == ["gh", "llm", "--version"]:
            return FakeCompletedProcess(f"{entrypoint_version}\n")
        if cmd == ["gh", "--version"]:
            return FakeCompletedProcess("gh version test-build\n")
        if cmd == ["gh", "auth", "status", "--active", "--hostname", "github.example.com"]:
            return FakeCompletedProcess("github.example.com\n  ✓ Logged in to github.example.com account neko\n")
        if cmd == ["gh", "api", "user"]:
            return FakeCompletedProcess(json.dumps({"login": "ShigureNyako"}))
        if cmd[:3] == ["gh", "api", "graphql"]:
            return FakeCompletedProcess(json.dumps({"data": {"viewer": {"login": "ShigureNyako"}}}))
        if cmd == ["gh", "auth", "status"]:
            return FakeCompletedProcess("expired other host", returncode=1, stderr="expired other host")
        return FakeCompletedProcess("", returncode=1, stderr="unexpected command")

    monkeypatch.setattr(doctor_commands.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")
    monkeypatch.setenv("GH_HOST", "github.example.com")
    monkeypatch.setattr(sys, "argv", ["gh-llm"])

    code = cli.run(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "- target_host: github.example.com" in out
    assert "gh auth status --active --hostname github.example.com" in out
    assert ["gh", "auth", "status", "--active", "--hostname", "github.example.com"] in calls
    assert ["gh", "auth", "status"] not in calls


def test_parse_event_indexes_batch() -> None:
    assert cli.parse_event_indexes(["5,11", "8-6"]) == [5, 6, 7, 8, 11]


def test_parse_review_ids_batch() -> None:
    assert cli.parse_review_ids(["PRR_a,PRR_b", "PRR_b", "PRR_c"]) == ["PRR_a", "PRR_b", "PRR_c"]


def test_view_and_expand_use_real_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0

    out = capsys.readouterr().out
    assert "## Description" in out
    assert "This is PR description" in out
    assert "Reactions: 🚀 x1" in out
    assert "gh pr edit 77928 --repo PaddlePaddle/Paddle --body '<pr_description_markdown>'" in out
    assert "Δ PR diff: `gh pr diff 77928 --repo PaddlePaddle/Paddle`" in out
    assert "### Page 1/4" in out
    assert "Reactions: 👍 x2" in out
    assert "### Page 3/4" in out
    assert "### Page 4/4" in out
    assert "Hidden timeline page: 2" in out
    assert "---" in out
    assert "gh-llm pr timeline-expand 2 --pr 77928 --repo PaddlePaddle/Paddle" in out
    assert "## Actions" in out
    assert "## Checks" in out
    assert "[IN_PROGRESS/NONE] unit-tests (check-run)" in out
    assert "passed checks hidden." in out
    assert "gh pr comment 77928 --repo PaddlePaddle/Paddle --body '<comment_body>'" in out
    assert "gh pr comment 77928 --repo PaddlePaddle/Paddle --body-file <path-or->" in out
    assert "gh pr close 77928 --repo PaddlePaddle/Paddle" in out
    assert "gh pr edit 77928 --repo PaddlePaddle/Paddle --add-label '<label1>,<label2>'" in out
    assert "gh pr edit 77928 --repo PaddlePaddle/Paddle --remove-label '<label1>,<label2>'" in out
    assert "gh pr edit 77928 --repo PaddlePaddle/Paddle --add-reviewer '<reviewer1>,<reviewer2>'" in out
    assert "gh pr edit 77928 --repo PaddlePaddle/Paddle --add-assignee '<assignee1>,<assignee2>'" in out
    assert "link:" not in out
    assert (
        "Edit comment via gh-llm: `gh-llm pr comment-edit c3 --body '<comment_body>' --pr 77928 --repo PaddlePaddle/Paddle`"
        in out
    )
    assert (
        "Multi-line edit via gh-llm: `gh-llm pr comment-edit c3 --body-file <comment.md> --pr 77928 --repo PaddlePaddle/Paddle`"
        in out
    )

    pre_expand_calls = len(responder.calls)
    code = cli.run(["pr", "timeline-expand", "2", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0

    out = capsys.readouterr().out
    assert "### Page 2/4" in out
    assert "commit 2" in out
    assert (
        "Δ commit diff: `gh api repos/PaddlePaddle/Paddle/commits/oid-2 -H 'Accept: application/vnd.github.v3.diff'`"
        in out
    )

    code = cli.run(["pr", "timeline-expand", "3", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "### Page 3/4" in out
    assert "(review hidden: outdated)" in out
    assert "Review comments (1/3 shown):" not in out
    assert "Thread[1] PRRT_mock_1" not in out
    assert "1 resolved review comments are collapsed;" not in out
    assert "thread_id: PRRT_mock_1" not in out
    assert "Reply via gh: `gh api graphql" not in out
    assert "Resolve via gh: `gh api graphql" not in out

    code = cli.run(
        ["pr", "review-expand", "PRR_mock", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "## Timeline Event" in out
    assert "lgtm" in out
    assert "Review comments (3/3 shown):" in out
    assert "PRRT_mock_1" in out
    assert "The error message could be more helpful." in out
    assert "Reactions: ❤️ x1" in out
    assert "[outdated] python/test_file.py:L23" in out
    assert "Suggested Change:" in out
    assert "@@ python/test_file.py:L22 @@" in out
    assert "+new_api_call()" in out
    assert (
        "Edit comment via gh-llm: `gh-llm pr comment-edit PRRC_self_1 --body '<comment_body>' --pr 77928 --repo PaddlePaddle/Paddle`"
        in out
    )
    assert "reply_body_file: '<reply.md>'" in out
    assert (
        "Multi-line reply via gh-llm: `gh-llm pr thread-reply PRRT_mock_1 --body-file <reply.md> --pr 77928 --repo PaddlePaddle/Paddle`"
        in out
    )
    assert (
        "Multi-line edit via gh-llm: `gh-llm pr comment-edit PRRC_self_1 --body-file <comment.md> --pr 77928 --repo PaddlePaddle/Paddle`"
        in out
    )
    assert "Unresolve via gh-llm:" in out

    code = cli.run(
        [
            "pr",
            "review-expand",
            "PRR_mock",
            "--threads",
            "1-1",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "## Review PRR_mock" in out
    assert "Review comments (2/2 shown):" in out
    assert "Thread[1] PRRT_mock_1" in out
    assert "PRRT_mock_2" not in out

    code = cli.run(["pr", "checks", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--all"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Checks" in out
    assert "[COMPLETED/SUCCESS] lint (check-run)" in out
    assert "gh run view 101 --log --repo PaddlePaddle/Paddle" in out
    assert "gh run view 202 --job 303 --log --repo PaddlePaddle/Paddle" in out

    code = cli.run(
        [
            "pr",
            "thread-reply",
            "PRRT_mock_1",
            "--body",
            "please update",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "thread: PRRT_mock_1" in out
    assert "reply_comment_id: PRRC_reply_1" in out
    assert "status: replied" in out

    code = cli.run(
        [
            "pr",
            "thread-resolve",
            "PRRT_mock_1",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "thread: PRRT_mock_1" in out
    assert "status: resolved" in out

    code = cli.run(
        [
            "pr",
            "comment-edit",
            "c3",
            "--body",
            "updated body",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "comment: c3" in out
    assert "status: edited" in out

    code = cli.run(
        [
            "pr",
            "thread-unresolve",
            "PRRT_mock_2",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "thread: PRRT_mock_2" in out
    assert "status: unresolved" in out

    expand_calls = responder.calls[pre_expand_calls:]
    assert any(call[:3] == ["gh", "pr", "view"] for call in expand_calls)
    assert any(call[:3] == ["gh", "api", "graphql"] for call in expand_calls)

    code = cli.run(["pr", "comment-expand", "c1", "--pr", "77928", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Comment c1" in out
    assert "- Type: IssueComment" in out
    assert "END_MARKER" in out


def test_pr_view_after_filters_incremental_events_and_avoids_forward_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "view",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--after",
            "2026-02-14T14:44:36Z",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "fetched_at: " in out
    assert "timeline_after: 2026-02-14T14:44:36Z" in out
    assert "timeline_events: 3" in out
    assert "timeline_events_unfiltered: 7" in out
    assert "### Page 1/2" in out
    assert "### Page 2/2" in out
    assert "5. [2026-02-14 14:51 UTC] review/approved by @reviewer" in out
    assert "7. [2026-02-14 15:11 UTC] comment by @ShigureNyako" in out
    assert "commit 2" not in out
    assert "comment 2" not in out

    timeline_queries = [
        _extract_form(call, "query")
        for call in responder.calls
        if call[:3] == ["gh", "api", "graphql"] and "timelineItems(" in _extract_form(call, "query")
    ]
    assert any("timelineItems(last:" in query for query in timeline_queries)
    assert not any("timelineItems(first:" in query for query in timeline_queries)


def test_pr_view_after_keeps_full_thread_context_for_window_matched_review_updates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def thread_spanning_events() -> list[dict[str, Any]]:
        return [
            {
                "__typename": "PullRequestReview",
                "id": "PRR_old",
                "submittedAt": "2026-02-14T14:40:00Z",
                "state": "CHANGES_REQUESTED",
                "body": "older review body",
                "isMinimized": False,
                "minimizedReason": None,
                "author": {"login": "reviewer"},
            },
            {
                "__typename": "IssueComment",
                "id": "c_old",
                "url": "https://example.com/c-old",
                "createdAt": "2026-02-14T14:54:00Z",
                "body": "older unrelated comment",
                "author": {"login": "someone"},
                "reactionGroups": [],
            },
            {
                "__typename": "IssueComment",
                "id": "c_new",
                "url": "https://example.com/c-new",
                "createdAt": "2026-02-14T15:10:00Z",
                "body": "fresh comment",
                "author": {"login": "ShigureNyako"},
                "reactionGroups": [],
            },
        ]

    def spanning_thread_payload(after: str | None) -> dict[str, Any]:
        if after is not None:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "PRRT_spanning",
                                    "isResolved": False,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "id": "rc_old",
                                                "path": "python/test_file.py",
                                                "body": "root review comment",
                                                "line": 21,
                                                "originalLine": 21,
                                                "startLine": None,
                                                "originalStartLine": None,
                                                "diffHunk": "@@ -20,1 +20,1 @@\n-old_name\n+new_name",
                                                "createdAt": "2026-02-14T14:40:01Z",
                                                "outdated": False,
                                                "isMinimized": False,
                                                "minimizedReason": None,
                                                "author": {"login": "reviewer"},
                                                "reactionGroups": [],
                                                "pullRequestReview": {"id": "PRR_old"},
                                            },
                                            {
                                                "id": "rc_new",
                                                "path": "python/test_file.py",
                                                "body": "reply inside window",
                                                "line": 21,
                                                "originalLine": 21,
                                                "startLine": None,
                                                "originalStartLine": None,
                                                "diffHunk": "@@ -20,1 +20,1 @@\n-old_name\n+new_name",
                                                "createdAt": "2026-02-14T15:00:02Z",
                                                "outdated": False,
                                                "isMinimized": False,
                                                "minimizedReason": None,
                                                "author": {"login": "ShigureNyako"},
                                                "reactionGroups": [],
                                                "pullRequestReview": {"id": "PRR_followup"},
                                            },
                                        ]
                                    },
                                }
                            ],
                        }
                    }
                }
            }
        }

    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys.modules[__name__], "_events", thread_spanning_events)
    monkeypatch.setattr(sys.modules[__name__], "_review_threads_payload", spanning_thread_payload)

    code = cli.run(
        [
            "pr",
            "view",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "1",
            "--show",
            "timeline",
            "--after",
            "2026-02-14T14:55:00Z",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "[2026-02-14 14:40 UTC] review/changes_requested by @reviewer" in out
    assert "older unrelated comment" not in out
    assert "root review comment" in out
    assert "reply inside window" in out
    assert "[before selected window]" in out
    assert "[within selected window]" in out
    assert "Thread[1] PRRT_spanning (1 update in selected window; full context shown)" in out


def test_issue_view_before_filters_older_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "issue",
            "view",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--before",
            "2026-02-13T13:30:00Z",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "timeline_before: 2026-02-13T13:30:00Z" in out
    assert "timeline_events: 3" in out
    assert "timeline_events_unfiltered: 5" in out
    assert "### Page 1/2" in out
    assert "### Page 2/2" in out
    assert "3. [2026-02-13 13:00 UTC] comment by @ShigureNyako" in out
    assert "issue/closed by @ShigureNyako" not in out
    assert "issue/marked-as-duplicate" not in out

    timeline_queries = [
        _extract_form(call, "query")
        for call in responder.calls
        if call[:3] == ["gh", "api", "graphql"] and "timelineItems(" in _extract_form(call, "query")
    ]
    assert any("timelineItems(first:" in query for query in timeline_queries)
    assert not any("timelineItems(last:" in query for query in timeline_queries)


def test_pr_timeline_expand_with_after_uses_filtered_page_numbers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "pr",
            "timeline-expand",
            "2",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--after",
            "2026-02-14T14:44:36Z",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "timeline_after: 2026-02-14T14:44:36Z" in out
    assert "### Page 2/2" in out
    assert "7. [2026-02-14 15:11 UTC] comment by @ShigureNyako" in out
    assert "review/APPROVED by @reviewer" not in out
    assert "comment-edit c3 --body '<comment_body>' --pr 77928 --repo PaddlePaddle/Paddle" in out


def test_pr_timeline_expand_with_after_and_expand_option(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "pr",
            "timeline-expand",
            "1",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--after",
            "2026-02-14T14:44:36Z",
            "--expand",
            "resolved,minimized",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "timeline_after: 2026-02-14T14:44:36Z" in out
    assert "### Page 1/2" in out
    assert "Review comments (3/3 shown):" in out
    assert "PRRT_mock_1" in out
    assert "PRRT_mock_2" in out
    assert "(review hidden: outdated)" not in out
    assert "resolved review comments are collapsed" not in out
    assert "hidden review comments are collapsed" not in out


def test_invalid_timeline_window_range_reports_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "issue",
            "view",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
            "--after",
            "2026-02-13T14:10:00Z",
            "--before",
            "2026-02-13T14:10:00Z",
        ]
    )
    assert code == 1

    err = capsys.readouterr().err
    assert "error: invalid time range: `--after` must be earlier than `--before`" in err
    assert responder.calls == []


def test_pr_view_show_meta_skips_timeline_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--show", "meta"])
    assert code == 0

    out = capsys.readouterr().out
    assert "pr: 77928" in out
    assert "timeline_events:" not in out
    assert "## Timeline" not in out
    assert "## Checks" not in out

    graphql_queries = [_extract_form(call, "query") for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    assert any("headRefName" in query and "timelineItems" not in query for query in graphql_queries)
    assert not any("timelineItems(" in query for query in graphql_queries)
    assert not any("reviewThreads(first:100" in query for query in graphql_queries)
    assert not any("statusCheckRollup" in query for query in graphql_queries)


def test_pr_view_show_checks_fetches_checks_without_timeline_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--show", "checks"])
    assert code == 0

    out = capsys.readouterr().out
    assert "## Checks" in out
    assert "[IN_PROGRESS/NONE] unit-tests (check-run)" in out
    assert "## Timeline" not in out

    graphql_queries = [_extract_form(call, "query") for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    assert any("statusCheckRollup" in query for query in graphql_queries)
    assert not any("timelineItems(" in query for query in graphql_queries)
    assert not any("reviewThreads(first:100" in query for query in graphql_queries)


def test_pr_checks_command_skips_timeline_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["pr", "checks", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--all"])
    assert code == 0

    out = capsys.readouterr().out
    assert "## Checks" in out
    assert "[COMPLETED/SUCCESS] lint (check-run)" in out

    graphql_queries = [_extract_form(call, "query") for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    assert any("statusCheckRollup" in query for query in graphql_queries)
    assert not any("timelineItems(" in query for query in graphql_queries)
    assert not any("reviewThreads(first:100" in query for query in graphql_queries)


def test_pr_view_show_mergeability_fetches_status_without_timeline_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["pr", "view", "77971", "--repo", "PaddlePaddle/Paddle", "--show", "mergeability"])
    assert code == 0

    out = capsys.readouterr().out
    assert "## Mergeability" in out
    assert "Status: Merging is blocked" in out
    assert "## Timeline" not in out

    graphql_queries = [_extract_form(call, "query") for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    assert any("statusCheckRollup" in query for query in graphql_queries)
    assert not any("timelineItems(" in query for query in graphql_queries)
    assert not any("reviewThreads(first:100" in query for query in graphql_queries)


def test_web_like_extra_timeline_events_are_rendered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys.modules[__name__], "_events", _events_with_web_like_extras)

    code = cli.run(["pr", "timeline-expand", "4", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "reference" in out
    assert 'PR #77887 "Test referenced PR" by @alice' in out
    assert "gh-llm pr view 77887 --repo PaddlePaddle/Paddle" in out

    code = cli.run(["pr", "timeline-expand", "5", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "cross-reference" in out
    assert "cross-reference by @triager (Tri Ager)" in out
    assert 'issue #12345 "Test issue" by @bob (Bob)' in out
    assert "gh-llm issue view 12345 --repo PaddlePaddle/Paddle" in out

    code = cli.run(["pr", "timeline-expand", "6", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "label/remove" in out
    assert "push/force" in out


def test_title_renamed_event_is_rendered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys.modules[__name__], "_events", _events_with_web_like_extras)

    code = cli.run(["pr", "timeline-expand", "8", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "pr/title-edited" in out
    assert "title changed" in out
    assert "from: Old title" in out
    assert "to: New title" in out


def test_review_dismissed_event_is_rendered_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys.modules[__name__], "_events", _events_with_web_like_extras)

    code = cli.run(["pr", "timeline-expand", "7", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "push/commit" in out
    assert "review/dismissed" in out
    assert "dismissed their stale review" in out
    assert out.index("push/commit") < out.index("review/dismissed")

    graphql_queries = [_extract_form(call, "query") for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    assert any("REVIEW_DISMISSED_EVENT" in query for query in graphql_queries)


def test_issue_view_summary_and_actions_skip_timeline_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["issue", "view", "77924", "--repo", "PaddlePaddle/Paddle", "--show", "summary,actions"])
    assert code == 0

    out = capsys.readouterr().out
    assert "## Description" in out
    assert "## Actions" in out
    assert "## Timeline" not in out
    assert "timeline_events:" not in out

    graphql_queries = [_extract_form(call, "query") for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    assert not any("timelineItems(" in query for query in graphql_queries)


def test_issue_view_and_expand_use_real_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["issue", "view", "77924", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "issue: 77924" in out
    assert "## Description" in out
    assert "This is issue description" in out
    assert "Reactions: 👀 x1" in out
    assert "gh issue edit 77924 --repo PaddlePaddle/Paddle --body '<issue_description_markdown>'" in out
    assert "## Diff Actions" not in out
    assert "### Page 1/3" in out
    assert "### Page 2/3" in out
    assert "### Page 3/3" in out
    assert "Hidden timeline page" not in out
    assert "(comment hidden: outdated)" in out
    assert "run `gh-llm issue comment-expand ic1 --issue 77924 --repo PaddlePaddle/Paddle` for full comment" in out
    assert "## Actions" in out
    assert "gh issue comment 77924 --repo PaddlePaddle/Paddle --body '<comment_body>'" in out
    assert "gh issue comment 77924 --repo PaddlePaddle/Paddle --body-file <path-or->" in out
    assert "gh issue close 77924 --repo PaddlePaddle/Paddle" in out
    assert "gh issue edit 77924 --repo PaddlePaddle/Paddle --add-label '<label1>,<label2>'" in out
    assert "gh issue edit 77924 --repo PaddlePaddle/Paddle --remove-label '<label1>,<label2>'" in out
    assert "gh issue edit 77924 --repo PaddlePaddle/Paddle --add-assignee '<assignee1>,<assignee2>'" in out
    assert (
        "Edit comment via gh-llm: `gh-llm issue comment-edit ic2 --body '<comment_body>' --issue 77924 --repo PaddlePaddle/Paddle`"
        in out
    )
    assert (
        "Multi-line edit via gh-llm: `gh-llm issue comment-edit ic2 --body-file <comment.md> --issue 77924 --repo PaddlePaddle/Paddle`"
        in out
    )
    assert "cross-reference by @alice (Alice)" in out
    assert "gh-llm pr view 77900 --repo PaddlePaddle/Paddle" in out
    assert "issue/closed by @ShigureNyako" in out
    assert "issue/marked-as-duplicate by @SigureMo (Nyakku Shigure)" in out
    assert (
        'marked issue #77925 "Duplicate issue" by @alice (Alice) (PaddlePaddle/Paddle) as duplicate of this issue'
        in out
    )
    assert "gh-llm issue view 77925 --repo PaddlePaddle/Paddle" in out

    code = cli.run(
        ["issue", "timeline-expand", "2", "--issue", "77924", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "### Page 2/3" in out

    code = cli.run(["issue", "comment-expand", "ic1", "--issue", "77924", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Comment ic1" in out
    assert "- Type: IssueComment" in out


def test_extract_diff_hunks_prefers_first_added_line_for_right_side() -> None:
    diff = "\n".join(
        [
            "diff --git a/paddle/phi/kernels/funcs/abs.h b/paddle/phi/kernels/funcs/abs.h",
            "index 1111111..2222222 100644",
            "--- a/paddle/phi/kernels/funcs/abs.h",
            "+++ b/paddle/phi/kernels/funcs/abs.h",
            "@@ -22,6 +22,12 @@",
            ' #include "paddle/phi/common/amp_type_traits.h"',
            ' #include "paddle/phi/core/dense_tensor.h"',
            '+#include "paddle/phi/core/kernel_utils.h"',
            '+#include "paddle/phi/core/tensor_utils.h"',
            " template <typename T, typename Context>",
            "+inline void CheckInput(const DenseTensor& x) {}",
        ]
    )

    hunks = pr_commands._extract_diff_hunks(diff)  # pyright: ignore[reportPrivateUsage]

    assert len(hunks) == 1
    assert hunks[0].path == "paddle/phi/kernels/funcs/abs.h"
    assert hunks[0].anchor_line == 24


def test_extract_diff_hunks_uses_real_new_file_line_numbers_on_right_side() -> None:
    diff = "\n".join(
        [
            "diff --git a/src/gh_llm/commands/pr.py b/src/gh_llm/commands/pr.py",
            "index 1111111..2222222 100644",
            "--- a/src/gh_llm/commands/pr.py",
            "+++ b/src/gh_llm/commands/pr.py",
            "@@ -642,7 +642,7 @@ def cmd_pr_review_start(args: Any) -> int:",
            '-        print(f"Suggested anchor line (RIGHT): {hunk.anchor_line}")',
            '+        print(f"Suggested anchor line (RIGHT, first added line when available): {hunk.anchor_line}")',
            "         comment_cmd = display_command_with(",
            "             f\"pr review-comment --path '{hunk.path}' --line {hunk.anchor_line} --side RIGHT --body '<review_comment>' --pr {meta.ref.number} --repo {repo}\"",
            "         )",
            "         suggest_cmd = display_command_with(",
            "             f\"pr review-suggest --path '{hunk.path}' --line {hunk.anchor_line} --side RIGHT --body '<reason>' --suggestion '<replacement>' --pr {meta.ref.number} --repo {repo}\"",
            "         )",
        ]
    )

    hunks = pr_commands._extract_diff_hunks(diff)  # pyright: ignore[reportPrivateUsage]

    assert len(hunks) == 1
    assert hunks[0].path == "src/gh_llm/commands/pr.py"
    assert hunks[0].anchor_line == 642
    assert 642 in hunks[0].right_commentable_lines
    assert min(hunks[0].right_commentable_lines) == 642


def test_render_numbered_hunk_lines_preserves_real_right_side_line_numbers() -> None:
    hunk = pr_commands._DiffHunk(  # pyright: ignore[reportPrivateUsage]
        path="src/gh_llm/commands/pr.py",
        header="@@ -890,6 +890,7 @@ def _extract_diff_hunks(diff: str) -> list[_DiffHunk]:",
        anchor_line=893,
        lines=[
            "@@ -890,6 +890,7 @@ def _extract_diff_hunks(diff: str) -> list[_DiffHunk]:",
            "     current_hunk_lines: list[str] = []",
            "     current_old_line = 0",
            "     current_new_line = 0",
            "+    current_right_display_line = 0",
            "     current_anchor = 0",
            "     current_fallback_anchor = 0",
            "     current_left_commentable_lines: set[int] = set()",
        ],
        left_commentable_lines={890, 891, 892, 893, 894, 895},
        right_commentable_lines={890, 891, 892, 893, 894, 895, 896},
        match_paths={"src/gh_llm/commands/pr.py"},
    )

    rendered = pr_commands._render_numbered_hunk_lines(hunk)  # pyright: ignore[reportPrivateUsage]

    assert "L 890 R 890 |      current_hunk_lines: list[str] = []" in rendered
    assert "L 891 R 891 |      current_old_line = 0" in rendered
    assert "L 892 R 892 |      current_new_line = 0" in rendered
    assert "L     R 893 | +    current_right_display_line = 0" in rendered


def test_inline_review_thread_blocks_do_not_fallback_from_current_right_anchor_to_original_left_line() -> None:
    current_hunk = pr_commands._DiffHunk(  # pyright: ignore[reportPrivateUsage]
        path="paddle/phi/api/include/compat/ATen/ops/from_blob.h",
        header="@@ -18,3 +80,4 @@",
        anchor_line=81,
        lines=[
            "@@ -18,3 +80,4 @@",
            " context_before()",
            '+    PD_CHECK(storage_offset_.value() == 0, "storage_offset` should be zero.");',
            " context_after()",
        ],
        left_commentable_lines={18, 19},
        right_commentable_lines={80, 81, 82},
        match_paths={"paddle/phi/api/include/compat/ATen/ops/from_blob.h"},
    )
    stale_hunk = pr_commands._DiffHunk(  # pyright: ignore[reportPrivateUsage]
        path="paddle/phi/api/include/compat/ATen/ops/from_blob.h",
        header="@@ -80,4 +210,1 @@",
        anchor_line=210,
        lines=[
            "@@ -80,4 +210,1 @@",
            "-      sizes._PD_ToPaddleIntArray(),",
            "-      compat::_PD_AtenScalarTypeToPhiDataType(options.dtype()),",
            "-      phi::DataLayout::NCHW,",
            "-      options._PD_GetPlace());",
            "+  return for_blob(data, sizes).options(options).make_tensor();",
        ],
        left_commentable_lines={80, 81, 82, 83},
        right_commentable_lines={210},
        match_paths={"paddle/phi/api/include/compat/ATen/ops/from_blob.h"},
    )
    summary = ReviewThreadSummary(
        thread_id="PRRT_mock_current",
        path="paddle/phi/api/include/compat/ATen/ops/from_blob.h",
        is_resolved=False,
        comment_count=1,
        is_outdated=False,
        anchor_side="RIGHT",
        anchor_line=81,
        right_lines=(81,),
        left_lines=(81,),
        display_ref="R81",
        comments=(),
    )

    blocks_by_hunk = pr_commands._build_inline_review_thread_blocks_for_file(  # pyright: ignore[reportPrivateUsage]
        hunks=[current_hunk, stale_hunk],
        summaries=[summary],
        extra_contexts=[None, None],
    )

    assert ("RIGHT", 81) in blocks_by_hunk[0]
    assert "💬 thread PRRT_mock_current at R81 (1 comment)" in blocks_by_hunk[0][("RIGHT", 81)]
    assert blocks_by_hunk[1] == {}


def test_pr_review_actions_for_llm_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(["pr", "review-start", "--pr", "77928", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Review Start" in out
    assert "Head snapshot: 3333333333333333333333333333333333333333" in out
    assert "Files changed: 1" in out
    assert "File page: 1/1 (1-1 of 1)" in out
    assert "Hunks on this page: 1" in out
    assert "gh pr diff 77928 --repo PaddlePaddle/Paddle" in out
    assert (
        "gh-llm pr review-comment --path '<path>' --line <line> --side RIGHT --body '<review_comment>' --head 3333333333333333333333333333333333333333 --pr 77928 --repo PaddlePaddle/Paddle"
        in out
    )
    assert (
        "gh-llm pr review-suggest --path '<path>' --line <line> --side RIGHT --body '<reason>' --suggestion '<replacement>' --head 3333333333333333333333333333333333333333 --pr 77928 --repo PaddlePaddle/Paddle"
        in out
    )
    assert (
        "gh-llm pr review-comment --path '<path>' --start-line <start_line> --line <line> --side RIGHT --body '<review_comment>' --head 3333333333333333333333333333333333333333 --pr 77928 --repo PaddlePaddle/Paddle"
        in out
    )
    assert "gh-llm pr thread-expand <thread_id> --pr 77928 --repo PaddlePaddle/Paddle" in out
    assert "### File 1/1: python/test_file.py" in out
    assert "Status: modified (+1 -1, 2 changes)" in out
    assert "Existing review threads in this file: 2 (1 active, 1 resolved)" in out
    assert "LEFT commentable span(s): 20" in out
    assert "RIGHT commentable span(s): 20" in out
    assert "Related review threads in this hunk:" not in out
    assert "Use the L#### / R#### labels from the numbered diff below as --line values." in out
    assert "For a continuous multi-line range on the same side, add --start-line <start_line>." in out
    assert "@@ -20,2 +20,2 @@ def demo():" in out
    assert "L  20 R     | -old_api_call()" in out
    assert "L     R  20 | +new_api_call()" in out
    assert "Suggested anchor line" not in out

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body",
            "please simplify",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "thread: PRRT_new_1" in out
    assert "comment: PRRC_new_1" in out
    assert "status: commented" in out

    code = cli.run(
        [
            "pr",
            "review-suggest",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body",
            "nits",
            "--suggestion",
            "new_api_call()",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "status: suggested" in out

    code = cli.run(
        [
            "pr",
            "review-submit",
            "--event",
            "REQUEST_CHANGES",
            "--body",
            "please address comments",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "review: PRR_pending_1" in out
    assert "state: COMMENTED" in out
    assert "status: submitted" in out


def test_pr_review_start_shows_numbered_right_side_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_leading_context(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/pulls/" in cmd[2] and "/files?" in cmd[2]:
            return FakeCompletedProcess(
                json.dumps(
                    [
                        {
                            "filename": "python/test_file.py",
                            "status": "modified",
                            "additions": 1,
                            "deletions": 0,
                            "changes": 1,
                            "patch": "\n".join(
                                [
                                    "@@ -20,3 +20,4 @@ def demo():",
                                    " context_before()",
                                    "+new_api_call()",
                                    " context_after()",
                                ]
                            ),
                        }
                    ]
                )
            )
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_leading_context)

    code = cli.run(["pr", "review-start", "--pr", "77928", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Existing review threads in this file: 2 (1 active, 1 resolved)" in out
    assert "Related review threads in this hunk:" not in out
    assert "            ┆ 💬 thread PRRT_mock_1 at R21-23 (2 comments)" in out
    assert "            ┆ ↳ [1] @reviewer: use clear variable names" in out
    assert "            ┆ ↳ [2] @ShigureNyako [outdated]: self reply" in out
    assert "            ┆ ✓ resolved thread PRRT_mock_2 at R22 (1 comment)" in out
    assert "            ┆ ↳ [1] @reviewer: The error message could be more helpful. ..." in out
    assert "LEFT commentable span(s): 20-21" in out
    assert "RIGHT commentable span(s): 20-22" in out
    assert "L  20 R  20 |  context_before()" in out
    assert "L     R  21 | +new_api_call()" in out
    assert "L  21 R  22 |  context_after()" in out


def test_pr_review_start_supports_extra_context_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    head_lines = [f"line_{index}" for index in range(1, 26)]
    head_lines[18] = "before_api()"
    head_lines[19] = "new_api_call()"
    head_lines[20] = "after_api()"

    def run_with_file_contents(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/contents/" in cmd[2]:
            payload = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode("\n".join(head_lines).encode("utf-8")).decode("ascii"),
            }
            return FakeCompletedProcess(json.dumps(payload))
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_file_contents)

    code = cli.run(["pr", "review-start", "--pr", "77928", "--repo", "PaddlePaddle/Paddle", "--context-lines", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Extra context lines: 1" in out
    assert "L  19 R  19 |  before_api()" in out
    assert "L     R  20 | +new_api_call()" in out
    assert "L  21 R  21 |  after_api()" in out


def test_pr_review_start_auto_shows_nearby_current_threads_and_hides_stale_outdated_threads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    head_lines = [f"line_{index}" for index in range(1, 26)]
    head_lines[18] = "before_api()"
    head_lines[19] = "new_api_call()"
    head_lines[20] = "after_api()"

    def run_with_context_threads(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/contents/" in cmd[2]:
            payload = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode("\n".join(head_lines).encode("utf-8")).decode("ascii"),
            }
            return FakeCompletedProcess(json.dumps(payload))
        if cmd[:3] == ["gh", "api", "graphql"]:
            query = _extract_form(cmd, "query")
            if "reviewThreads(first:100" in query:
                return FakeCompletedProcess(
                    json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "reviewThreads": {
                                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                                            "nodes": [
                                                {
                                                    "id": "PRRT_current_context",
                                                    "isResolved": False,
                                                    "comments": {
                                                        "nodes": [
                                                            {
                                                                "id": "rc_context",
                                                                "path": "python/test_file.py",
                                                                "body": "current context thread",
                                                                "line": 19,
                                                                "originalLine": 19,
                                                                "startLine": None,
                                                                "originalStartLine": None,
                                                                "diffHunk": "@@ -20,3 +20,4 @@ def demo():\n context_before()\n+new_api_call()\n context_after()",
                                                                "createdAt": "2026-02-14T14:50:01Z",
                                                                "outdated": True,
                                                                "isMinimized": False,
                                                                "minimizedReason": None,
                                                                "author": {"login": "reviewer"},
                                                                "reactionGroups": [],
                                                                "pullRequestReview": {"id": "PRR_mock"},
                                                            }
                                                        ]
                                                    },
                                                },
                                                {
                                                    "id": "PRRT_outdated_context",
                                                    "isResolved": False,
                                                    "comments": {
                                                        "nodes": [
                                                            {
                                                                "id": "rc_outdated",
                                                                "path": "python/test_file.py",
                                                                "body": "outdated context thread",
                                                                "line": None,
                                                                "originalLine": 19,
                                                                "startLine": None,
                                                                "originalStartLine": None,
                                                                "diffHunk": "@@ -20,3 +20,4 @@ def demo():\n context_before()\n+new_api_call()\n context_after()",
                                                                "createdAt": "2026-02-14T14:50:02Z",
                                                                "outdated": True,
                                                                "isMinimized": False,
                                                                "minimizedReason": None,
                                                                "author": {"login": "reviewer"},
                                                                "reactionGroups": [],
                                                                "pullRequestReview": {"id": "PRR_mock"},
                                                            }
                                                        ]
                                                    },
                                                },
                                            ],
                                        }
                                    }
                                }
                            }
                        }
                    )
                )
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_context_threads)

    code = cli.run(["pr", "review-start", "--pr", "77928", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Existing review threads in this file: 1 (1 active)" in out
    assert "PRRT_outdated_context" not in out
    assert "L  19 R  19 |  before_api()" in out
    assert "            ┆ 💬 thread PRRT_current_context at R19 (1 comment)" in out
    assert "            ┆ ↳ [1] @reviewer [outdated]: current context thread" in out


def test_pr_review_start_supports_changed_file_pagination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Files changed: 3" in out
    assert "Head snapshot: 3333333333333333333333333333333333333333" in out
    assert "File page: 1/2 (1-2 of 3)" in out
    assert "### File 1/3: .gitignore" in out
    assert "### File 2/3: paddle/phi/api/include/compat/ATen/core/TensorBase.h" in out
    assert "next file page" in out
    assert (
        "gh-llm pr review-start --page 2 --page-size 2 --max-hunks 40 --head 3333333333333333333333333333333333333333 --pr 78255 --repo PaddlePaddle/Paddle"
        in out
    )

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--context-lines",
            "1",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Extra context lines: 1" in out
    assert (
        "gh-llm pr review-start --page 2 --page-size 2 --max-hunks 40 --head 3333333333333333333333333333333333333333 --context-lines 1 --pr 78255 --repo PaddlePaddle/Paddle"
        in out
    )

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page",
            "2",
            "--page-size",
            "2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "File page: 2/2 (3-3 of 3)" in out
    assert "### File 3/3: paddle/phi/api/include/compat/ATen/core/TensorBody.h" in out
    assert "previous file page" in out


def test_pr_review_start_supports_path_focus_and_hunk_selection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--path",
            "TensorBody.h",
            "--hunks",
            "2-3",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Focused file: paddle/phi/api/include/compat/ATen/core/TensorBody.h (3/3)" in out
    assert "### File 3/3: paddle/phi/api/include/compat/ATen/core/TensorBody.h" in out
    assert "#### Hunk 2" in out
    assert "#### Hunk 3" in out
    assert "#### Hunk 1" not in out
    assert "next file page" not in out


def test_pr_review_start_supports_file_range_selection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--files",
            "2-3",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Selected files: 2-3 of 3" in out
    assert "### File 2/3: paddle/phi/api/include/compat/ATen/core/TensorBase.h" in out
    assert "### File 3/3: paddle/phi/api/include/compat/ATen/core/TensorBody.h" in out
    assert "### File 1/3: .gitignore" not in out
    assert (
        "gh-llm pr review-start --files 1 --max-hunks 40 --head 3333333333333333333333333333333333333333 --pr 78255 --repo PaddlePaddle/Paddle"
        in out
    )
    assert "next file selection" not in out
    assert "next file page" not in out


def test_pr_review_start_rejects_hunks_without_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--hunks",
            "2-3",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: `--hunks` requires `--path`" in err


def test_pr_review_start_rejects_files_with_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--files",
            "2-3",
            "--path",
            "TensorBody.h",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: `--files` cannot be combined with `--path`" in err


def test_pr_review_start_rejects_stale_head_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-start",
            "--pr",
            "78255",
            "--repo",
            "PaddlePaddle/Paddle",
            "--head",
            "deadbeef",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: stale review snapshot: requested --head deadbeef" in err
    assert "current head is 3333333333333333333333333333333333333333" in err
    assert "gh-llm pr review-start --pr 78255 --repo PaddlePaddle/Paddle" in err


def test_pr_review_comment_rejects_stale_head_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body",
            "please simplify",
            "--head",
            "deadbeef",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: stale review snapshot: requested --head deadbeef" in err


def test_pr_review_comment_invalid_location_error_is_precise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/test_file.py",
            "--line",
            "21",
            "--side",
            "RIGHT",
            "--body",
            "please simplify",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: line 21 on RIGHT is not a commentable diff line for python/test_file.py." in err
    assert "Try a line from the PR diff for that side instead (e.g. 20)." in err


def test_pr_review_comment_supports_multiline_right_side_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_leading_context(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "pr", "diff"]:
            return FakeCompletedProcess(
                "\n".join(
                    [
                        "diff --git a/python/test_file.py b/python/test_file.py",
                        "index 1111111..2222222 100644",
                        "--- a/python/test_file.py",
                        "+++ b/python/test_file.py",
                        "@@ -20,3 +20,4 @@ def demo():",
                        " context_before()",
                        "+new_api_call()",
                        " context_after()",
                    ]
                )
                + "\n"
            )
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_leading_context)

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/test_file.py",
            "--start-line",
            "21",
            "--line",
            "22",
            "--side",
            "RIGHT",
            "--body",
            "please review this range",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "thread: PRRT_new_1" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    review_call = next(call for call in graphql_calls if "addPullRequestReviewThread" in _extract_form(call, "query"))
    assert _extract_field(review_call, "startLine") == "21"
    assert _extract_field(review_call, "startSide") == "RIGHT"
    assert _extract_field(review_call, "line") == "22"
    assert _extract_field(review_call, "side") == "RIGHT"


def test_pr_review_comment_accepts_deleted_file_left_side(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_deleted_file_diff(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "pr", "diff"]:
            return FakeCompletedProcess(
                "\n".join(
                    [
                        "diff --git a/python/deleted_file.py b/python/deleted_file.py",
                        "deleted file mode 100644",
                        "index 1111111..0000000",
                        "--- a/python/deleted_file.py",
                        "+++ /dev/null",
                        "@@ -1,2 +0,0 @@",
                        "-old_api_call()",
                        "-legacy_cleanup()",
                    ]
                )
                + "\n"
            )
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_deleted_file_diff)

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/deleted_file.py",
            "--line",
            "1",
            "--side",
            "LEFT",
            "--body",
            "please confirm deletion",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "thread: PRRT_new_1" in out
    assert "status: commented" in out


def test_pr_review_comment_deleted_file_right_side_error_is_precise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_deleted_file_diff(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "pr", "diff"]:
            return FakeCompletedProcess(
                "\n".join(
                    [
                        "diff --git a/python/deleted_file.py b/python/deleted_file.py",
                        "deleted file mode 100644",
                        "index 1111111..0000000",
                        "--- a/python/deleted_file.py",
                        "+++ /dev/null",
                        "@@ -1,2 +0,0 @@",
                        "-old_api_call()",
                        "-legacy_cleanup()",
                    ]
                )
                + "\n"
            )
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_deleted_file_diff)

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/deleted_file.py",
            "--line",
            "1",
            "--side",
            "RIGHT",
            "--body",
            "this side should not be commentable",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: line 1 on RIGHT is not a commentable diff line for python/deleted_file.py." in err
    assert "The current diff has no commentable lines on RIGHT for that file." in err


def test_pr_review_comment_null_thread_error_is_precise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_null_thread(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "api", "graphql"] and "addPullRequestReviewThread" in _extract_form(cmd, "query"):
            return FakeCompletedProcess(json.dumps({"data": {"addPullRequestReviewThread": {"thread": None}}}))
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_null_thread)

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body",
            "please simplify",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "error: failed to create review thread: GitHub rejected the requested review location" in err
    assert "python/test_file.py:20 RIGHT" in err


def test_cli_unexpected_error_shows_issue_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = argparse.ArgumentParser(prog="gh-llm")

    def boom_handler(_: Any) -> int:
        raise KeyError("boom")

    parser.set_defaults(handler=boom_handler)

    monkeypatch.setattr(cli, "_build_parser", lambda: parser)
    monkeypatch.setattr(sys, "argv", ["gh-llm"])

    code = cli.run([])
    assert code == 1
    err = capsys.readouterr().err
    assert "unexpected error: 'boom'" in err
    assert "This is an unexpected gh-llm failure. Please consider reporting it." in err
    assert "Command: gh-llm" in err
    assert "⌨ issue_title: '<short summary>'" in err
    assert "⌨ issue_body: '<what happened, expected result, actual result>'" in err
    assert (
        "⏎ Report via gh: `gh issue create --repo ShigureLab/gh-llm --title '<short summary>' --body '<what happened, expected result, actual result>'`"
        in err
    )


def test_graphql_eof_retries_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    state = {"failed_once": False}

    def flaky_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "api", "graphql"] and not state["failed_once"]:
            state["failed_once"] = True
            return FakeCompletedProcess("", returncode=1, stderr='Post "https://api.github.com/graphql": EOF')
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(github_api.subprocess, "run", flaky_run)
    monkeypatch.setattr(github_api.time, "sleep", no_sleep)

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "### Page 1/4" in out
    assert state["failed_once"] is True


def test_graphql_eof_failure_prints_layered_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def failing_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "api", "graphql"]:
            return FakeCompletedProcess("", returncode=1, stderr='Post "https://api.github.com/graphql": EOF')
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(github_api.subprocess, "run", failing_run)
    monkeypatch.setattr(github_api.time, "sleep", no_sleep)
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error: GitHub GraphQL request failed after 4 attempts." in err
    assert 'Last error: Post "https://api.github.com/graphql": EOF' in err
    assert "Category: GraphQL transport / network" in err
    assert "Command: gh api graphql" in err
    assert "Try next:" in err
    assert "- gh auth status --active --hostname github.com" in err
    assert "- gh api user" in err
    assert "- gh api graphql -f query='query{viewer{login}}'" in err
    assert "- gh llm doctor" in err


def test_graphql_error_hints_scope_auth_status_to_target_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        del check, capture_output, text
        if cmd[:3] == ["gh", "api", "graphql"]:
            return FakeCompletedProcess("", returncode=1, stderr='Post "https://api.github.com/graphql": EOF')
        return FakeCompletedProcess("", returncode=1, stderr="unexpected command")

    def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(github_api.subprocess, "run", failing_run)
    monkeypatch.setattr(github_api.time, "sleep", no_sleep)
    monkeypatch.setenv("GH_HOST", "github.example.com")
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 1
    err = capsys.readouterr().err
    assert "- gh auth status --active --hostname github.example.com" in err
    assert "- gh llm doctor" in err


def test_pr_view_graphql_transport_error_uses_layered_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = {"attempts": 0}

    def failing_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        del check, capture_output, text
        if cmd[:3] == ["gh", "pr", "view"]:
            state["attempts"] += 1
            return FakeCompletedProcess("", returncode=1, stderr='Post "https://api.github.com/graphql": EOF')
        return FakeCompletedProcess("", returncode=1, stderr="unexpected command")

    def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(github_api.subprocess, "run", failing_run)
    monkeypatch.setattr(github_api.time, "sleep", no_sleep)
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 1
    assert state["attempts"] == 4
    err = capsys.readouterr().err
    assert "error: GitHub GraphQL request failed after 4 attempts." in err
    assert "Category: GraphQL transport / network" in err
    assert "Command: gh pr view" in err
    assert "- gh llm doctor" in err


def test_issue_view_graphql_transport_error_uses_layered_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = {"attempts": 0}

    def failing_run(cmd: list[str], *, check: bool, capture_output: bool, text: bool) -> FakeCompletedProcess:
        del check, capture_output, text
        if cmd[:3] == ["gh", "issue", "view"]:
            state["attempts"] += 1
            return FakeCompletedProcess("", returncode=1, stderr='Post "https://api.github.com/graphql": EOF')
        return FakeCompletedProcess("", returncode=1, stderr="unexpected command")

    def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(github_api.subprocess, "run", failing_run)
    monkeypatch.setattr(github_api.time, "sleep", no_sleep)
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")

    code = cli.run(["issue", "view", "77924", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 1
    assert state["attempts"] == 4
    err = capsys.readouterr().err
    assert "error: GitHub GraphQL request failed after 4 attempts." in err
    assert "Category: GraphQL transport / network" in err
    assert "Command: gh issue view" in err
    assert "- gh llm doctor" in err


def test_pr_review_submit_supports_body_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    body_file = tmp_path / "review.md"
    body_file.write_text("please address comments from file\n", encoding="utf-8")

    code = cli.run(
        [
            "pr",
            "review-submit",
            "--event",
            "REQUEST_CHANGES",
            "--body-file",
            str(body_file),
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: submitted" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    review_call = next(
        call
        for call in graphql_calls
        if (
            "submitPullRequestReview" in _extract_form(call, "query")
            or "addPullRequestReview" in _extract_form(call, "query")
        )
    )
    assert _extract_field(review_call, "body") == "please address comments from file\n"


def test_pr_review_submit_supports_body_file_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("stdin review body\n"))

    code = cli.run(
        [
            "pr",
            "review-submit",
            "--event",
            "COMMENT",
            "--body-file",
            "-",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: submitted" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    review_call = next(
        call
        for call in graphql_calls
        if (
            "submitPullRequestReview" in _extract_form(call, "query")
            or "addPullRequestReview" in _extract_form(call, "query")
        )
    )
    assert _extract_field(review_call, "body") == "stdin review body\n"


def test_pr_body_template_uses_repo_template_and_appends_missing_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    output_path = tmp_path / "pr-body.md"

    code = cli.run(
        [
            "pr",
            "body-template",
            "--repo",
            "ShigureLab/gh-llm",
            "--title",
            "feat: add PR body scaffold",
            "--requirements",
            "Motivation,Validation,Related Issues",
            "--output",
            str(output_path),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "## PR Body Scaffold" in out
    assert "template_found: true" in out
    assert "template_path: .github/PULL_REQUEST_TEMPLATE.md" in out
    assert 'required_sections: ["Motivation", "Validation", "Related Issues"]' in out
    assert 'added_sections: ["Validation", "Related Issues"]' in out
    assert "gh pr create --repo ShigureLab/gh-llm --title 'feat: add PR body scaffold' --body-file" in out

    body = output_path.read_text(encoding="utf-8")
    assert "## Motivation" in body
    assert "Template motivation section" in body
    assert "## Validation" in body
    assert "<!-- TODO: fill Validation -->" in body
    assert "## Related Issues" in body
    assert "<!-- TODO: fill Related Issues -->" in body


def test_pr_body_template_generates_scaffold_when_repo_has_no_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    output_path = tmp_path / "pr-body-no-template.md"

    code = cli.run(
        [
            "pr",
            "body-template",
            "--repo",
            "NoTemplateOrg/no-template",
            "--requirements",
            "改动动机,验证结果",
            "--output",
            str(output_path),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "template_found: false" in out
    assert "template_path: (none)" in out
    assert "⌨ pr_title: '<pr_title>'" in out
    assert 'added_sections: ["改动动机", "验证结果"]' in out
    assert "gh pr create --repo NoTemplateOrg/no-template --title '<pr_title>' --body-file" in out

    body = output_path.read_text(encoding="utf-8")
    assert body.startswith("## 改动动机")
    assert "<!-- TODO: fill 改动动机 -->" in body
    assert "## 验证结果" in body
    assert "<!-- TODO: fill 验证结果 -->" in body
    assert "## Motivation" not in body


def test_pr_body_template_finds_mixed_case_txt_template_via_directory_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    output_path = tmp_path / "pr-body-mixed-case.txt.md"

    code = cli.run(
        [
            "pr",
            "body-template",
            "--repo",
            "ExampleOrg/mixed-case-template",
            "--output",
            str(output_path),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "template_found: true" in out
    assert "template_path: .github/Pull_Request_Template.TXT" in out
    assert "Template from mixed-case txt" in output_path.read_text(encoding="utf-8")


def test_pr_body_template_finds_root_template_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    output_path = tmp_path / "pr-body-root-directory.md"

    code = cli.run(
        [
            "pr",
            "body-template",
            "--repo",
            "FQ-Studio-SV/SysAcopio",
            "--output",
            str(output_path),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "template_found: true" in out
    assert "template_path: PULL_REQUEST_TEMPLATE/template.md" in out
    assert "Root directory template body" in output_path.read_text(encoding="utf-8")


def test_pr_body_template_finds_docs_template_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    output_path = tmp_path / "pr-body-docs-directory.md"

    code = cli.run(
        [
            "pr",
            "body-template",
            "--repo",
            "DocsTemplateOrg/docs-template",
            "--output",
            str(output_path),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "template_found: true" in out
    assert "template_path: docs/PULL_REQUEST_TEMPLATE/bugfix.md" in out
    assert "Docs directory template body" in output_path.read_text(encoding="utf-8")


def test_decode_repository_contents_text_returns_none_for_invalid_base64() -> None:
    payload: dict[str, object] = {"encoding": "base64", "content": "A"}
    assert github_api._decode_repository_contents_text(payload) is None  # pyright: ignore[reportPrivateUsage]


def test_pr_body_template_surfaces_non_404_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_contents_failure(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and cmd[2].startswith("repos/BrokenOrg/broken-template/contents"):
            return FakeCompletedProcess("", returncode=1, stderr="HTTP 500: Internal Server Error")
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_contents_failure)
    output_path = tmp_path / "pr-body-broken.md"

    code = cli.run(
        [
            "pr",
            "body-template",
            "--repo",
            "BrokenOrg/broken-template",
            "--output",
            str(output_path),
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "error: HTTP 500: Internal Server Error" in err
    assert not output_path.exists()


def test_pr_thread_reply_supports_body_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    body_file = tmp_path / "reply.md"
    body_file.write_text("> quoted context\n\nreply from file\n", encoding="utf-8")

    code = cli.run(
        [
            "pr",
            "thread-reply",
            "PRRT_mock_1",
            "--body-file",
            str(body_file),
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: replied" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    reply_call = next(
        call for call in graphql_calls if "addPullRequestReviewThreadReply" in _extract_form(call, "query")
    )
    assert _extract_field(reply_call, "body") == "> quoted context\n\nreply from file\n"


def test_pr_review_comment_supports_body_file_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("stdin review comment\nwith second line\n"))

    code = cli.run(
        [
            "pr",
            "review-comment",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body-file",
            "-",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: commented" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    review_call = next(call for call in graphql_calls if "addPullRequestReviewThread" in _extract_form(call, "query"))
    assert _extract_field(review_call, "body") == "stdin review comment\nwith second line\n"


def test_pr_review_suggest_supports_body_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    body_file = tmp_path / "suggestion.md"
    body_file.write_text("nits from file\n", encoding="utf-8")

    code = cli.run(
        [
            "pr",
            "review-suggest",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body-file",
            str(body_file),
            "--suggestion",
            "new_api_call()",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: suggested" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    review_call = next(call for call in graphql_calls if "addPullRequestReviewThread" in _extract_form(call, "query"))
    assert _extract_field(review_call, "body") == "nits from file\n\n```suggestion\nnew_api_call()\n```"


def test_pr_thread_reply_rejects_body_and_body_file_together(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_file = tmp_path / "reply.md"
    body_file.write_text("reply from file\n", encoding="utf-8")

    try:
        cli.run(
            [
                "pr",
                "thread-reply",
                "PRRT_mock_1",
                "--body",
                "inline reply",
                "--body-file",
                str(body_file),
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected argparse to reject --body with --body-file")

    err = capsys.readouterr().err
    assert "argument -F/--body-file: not allowed with argument --body" in err


def test_repo_preflight_renders_onboarding_summary_and_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    code = cli.run(["repo", "preflight", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0

    out = capsys.readouterr().out
    assert "repo: PaddlePaddle/Paddle" in out
    assert "default_branch: develop" in out
    assert "viewer_permission: READ" in out
    assert "fork_recommended: true" in out
    assert "Description: PaddlePaddle core framework" in out
    assert "HTTPS clone: `https://github.com/PaddlePaddle/Paddle.git`" in out
    assert "Push access: no (viewer_permission: READ)" in out
    assert "CONTRIBUTING.md" in out
    assert "AGENTS.md" in out
    assert ".github/PULL_REQUEST_TEMPLATE.md" in out
    assert ".github/CODEOWNERS" in out
    assert "Matched rule: `develop`" in out
    assert "Required checks: `lint`, `unit-tests`" in out
    assert "Approving reviews: required (1)" in out
    assert "Code owner reviews: required" in out
    assert "release-gate" not in out
    assert "gh repo fork PaddlePaddle/Paddle --clone" in out
    assert "gh browse -R PaddlePaddle/Paddle --branch develop 'CONTRIBUTING.md'" in out
    assert "gh browse -R PaddlePaddle/Paddle --branch develop 'AGENTS.md'" in out
    assert "gh pr create --repo PaddlePaddle/Paddle --base develop" in out
    assert "gh-llm pr checks --pr <pr_number> --repo PaddlePaddle/Paddle" in out


def test_repo_preflight_uses_rest_branch_protection_when_rule_query_has_no_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_without_rule_match(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:3] == ["gh", "api", "graphql"] and "branchProtectionRules(first:100" in _extract_form(cmd, "query"):
            return FakeCompletedProcess(
                json.dumps(
                    {
                        "data": {
                            "repository": {
                                "branchProtectionRules": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [],
                                }
                            }
                        }
                    }
                )
            )
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_without_rule_match)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    code = cli.run(["repo", "preflight", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0

    out = capsys.readouterr().out
    assert "Protected branch: `develop`" in out
    assert "Required checks: `lint`, `unit-tests`" in out
    assert "Approving reviews: unknown" in out
    assert "Code owner reviews: unknown" in out
    assert "Admin enforcement: unknown" in out
    assert "gh-llm pr checks --pr <pr_number> --repo PaddlePaddle/Paddle" in out
    assert "Default branch `develop` is not protected." not in out


def test_repo_preflight_warns_on_truncated_tree_and_detects_contributing_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()

    def run_with_truncated_tree(
        cmd: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> FakeCompletedProcess:
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "/git/trees/" in cmd[2]:
            return FakeCompletedProcess(json.dumps({"sha": "mock-tree-sha", "truncated": True, "tree": []}))
        return responder.run(cmd, check=check, capture_output=capture_output, text=text)

    monkeypatch.setattr(github_api.subprocess, "run", run_with_truncated_tree)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    code = cli.run(["repo", "preflight", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0

    out = capsys.readouterr().out
    assert "tree_truncated: true" in out
    assert (
        "Warning: recursive repository tree output was truncated; onboarding file detection used common-path fallback and may still be incomplete."
        in out
    )
    assert "CONTRIBUTING_GUIDE.md" in out
    assert "gh browse -R PaddlePaddle/Paddle --branch develop 'CONTRIBUTING_GUIDE.md'" in out


def test_repo_preflight_surfaces_parent_repo_and_targets_upstream_pr_for_forks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    code = cli.run(["repo", "preflight", "--repo", "ShigureNyako/gh-llm"])
    assert code == 0

    out = capsys.readouterr().out
    assert "repo: ShigureNyako/gh-llm" in out
    assert "is_fork: true" in out
    assert "parent_repo: ShigureLab/gh-llm" in out
    assert "Parent repo: `ShigureLab/gh-llm`" in out
    assert "gh pr create --repo ShigureLab/gh-llm --base main" in out
    assert "gh pr create --repo ShigureNyako/gh-llm --base main" not in out


def _branch_protection_rules_payload(after: str | None) -> dict[str, Any]:
    del after
    return {
        "data": {
            "repository": {
                "branchProtectionRules": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "pattern": "release/*",
                            "requiresStatusChecks": True,
                            "requiredStatusCheckContexts": ["release-gate"],
                            "requiresApprovingReviews": True,
                            "requiredApprovingReviewCount": 2,
                            "requiresCodeOwnerReviews": False,
                            "isAdminEnforced": False,
                        },
                        {
                            "pattern": "develop",
                            "requiresStatusChecks": True,
                            "requiredStatusCheckContexts": ["lint", "unit-tests"],
                            "requiresApprovingReviews": True,
                            "requiredApprovingReviewCount": 1,
                            "requiresCodeOwnerReviews": True,
                            "isAdminEnforced": True,
                        },
                    ],
                },
            }
        }
    }


def _repo_view_payload(repo: str) -> dict[str, Any]:
    if repo == "ShigureNyako/gh-llm":
        return {
            "nameWithOwner": "ShigureNyako/gh-llm",
            "description": "Forked gh-llm workspace",
            "homepageUrl": "",
            "isFork": True,
            "parent": {
                "name": "gh-llm",
                "owner": {"login": "ShigureLab"},
            },
            "url": "https://github.com/ShigureNyako/gh-llm",
            "sshUrl": "git@github.com:ShigureNyako/gh-llm.git",
            "viewerPermission": "ADMIN",
            "defaultBranchRef": {"name": "main"},
        }
    return {
        "nameWithOwner": "PaddlePaddle/Paddle",
        "description": "PaddlePaddle core framework",
        "homepageUrl": "https://www.paddlepaddle.org.cn/",
        "isFork": False,
        "parent": None,
        "url": "https://github.com/PaddlePaddle/Paddle",
        "sshUrl": "git@github.com:PaddlePaddle/Paddle.git",
        "viewerPermission": "READ",
        "defaultBranchRef": {"name": "develop"},
    }


def _repo_tree_payload(path: str) -> dict[str, Any]:
    if "repos/ShigureNyako/gh-llm/" in path:
        return {
            "sha": "mock-tree-sha",
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob"},
                {"path": "skills/github-conversation/SKILL.md", "type": "blob"},
            ],
        }
    return {
        "sha": "mock-tree-sha",
        "truncated": False,
        "tree": [
            {"path": "README.md", "type": "blob"},
            {"path": "CONTRIBUTING.md", "type": "blob"},
            {"path": "AGENTS.md", "type": "blob"},
            {"path": ".github/PULL_REQUEST_TEMPLATE.md", "type": "blob"},
            {"path": ".github/CODEOWNERS", "type": "blob"},
            {"path": "docs/guide.md", "type": "blob"},
        ],
    }


def _repo_branch_payload(path: str) -> dict[str, Any] | None:
    if "/branches/" not in path:
        return None
    if "repos/ShigureNyako/gh-llm/branches/main" in path:
        return {
            "name": "main",
            "protected": False,
            "protection": {
                "enabled": False,
                "required_status_checks": {
                    "enforcement_level": "off",
                    "contexts": [],
                    "checks": [],
                },
            },
        }
    return {
        "name": "develop",
        "protected": True,
        "protection": {
            "enabled": True,
            "required_status_checks": {
                "enforcement_level": "non_admins",
                "contexts": ["lint", "unit-tests"],
                "checks": [
                    {"context": "lint", "app_id": None},
                    {"context": "unit-tests", "app_id": None},
                ],
            },
        },
    }


def test_pr_comment_edit_supports_body_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    body_file = tmp_path / "edit.md"
    body_file.write_text("updated body from file\n", encoding="utf-8")

    code = cli.run(
        [
            "pr",
            "comment-edit",
            "PRRC_self_1",
            "--body-file",
            str(body_file),
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: edited" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    edit_call = next(call for call in graphql_calls if "updatePullRequestReviewComment" in _extract_form(call, "query"))
    assert _extract_field(edit_call, "body") == "updated body from file\n"


def test_issue_comment_edit_supports_body_file_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("updated issue body from stdin\n"))

    code = cli.run(
        [
            "issue",
            "comment-edit",
            "ic2",
            "--body-file",
            "-",
            "--issue",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: edited" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    edit_call = next(call for call in graphql_calls if "updateIssueComment" in _extract_form(call, "query"))
    assert _extract_field(edit_call, "body") == "updated issue body from stdin\n"


def test_pr_comment_edit_rejects_body_and_body_file_together(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_file = tmp_path / "edit.md"
    body_file.write_text("updated body from file\n", encoding="utf-8")

    try:
        cli.run(
            [
                "pr",
                "comment-edit",
                "PRRC_self_1",
                "--body",
                "inline body",
                "--body-file",
                str(body_file),
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected argparse to reject --body with --body-file")

    err = capsys.readouterr().err
    assert "argument -F/--body-file: not allowed with argument --body" in err


def test_pr_review_suggest_supports_suggestion_file_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    reason_file = tmp_path / "reason.md"
    reason_file.write_text("nits from file\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin("replacement_from_stdin()\nsecond_line()\n"))

    code = cli.run(
        [
            "pr",
            "review-suggest",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body-file",
            str(reason_file),
            "--suggestion-file",
            "-",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: suggested" in out
    graphql_calls = [call for call in responder.calls if call[:3] == ["gh", "api", "graphql"]]
    review_call = next(call for call in graphql_calls if "addPullRequestReviewThread" in _extract_form(call, "query"))
    assert (
        _extract_field(review_call, "body")
        == "nits from file\n\n```suggestion\nreplacement_from_stdin()\nsecond_line()\n```"
    )


def test_pr_review_suggest_rejects_suggestion_and_suggestion_file_together(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suggestion_file = tmp_path / "replacement.txt"
    suggestion_file.write_text("replacement_from_file()\n", encoding="utf-8")

    try:
        cli.run(
            [
                "pr",
                "review-suggest",
                "--path",
                "python/test_file.py",
                "--line",
                "20",
                "--side",
                "RIGHT",
                "--suggestion",
                "inline_replacement()",
                "--suggestion-file",
                str(suggestion_file),
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected argparse to reject --suggestion with --suggestion-file")

    err = capsys.readouterr().err
    assert "argument --suggestion-file: not allowed with argument --suggestion" in err


def test_pr_review_suggest_rejects_dual_stdin_inputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responder = GhResponder()
    monkeypatch.setattr(github_api.subprocess, "run", responder.run)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("shared stdin\n"))

    code = cli.run(
        [
            "pr",
            "review-suggest",
            "--path",
            "python/test_file.py",
            "--line",
            "20",
            "--side",
            "RIGHT",
            "--body-file",
            "-",
            "--suggestion-file",
            "-",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "`--body-file -` cannot be combined with `--suggestion-file -`" in err
    assert not any(call[:3] == ["gh", "api", "graphql"] for call in responder.calls)


def _extract_form(cmd: list[str], key: str) -> str:
    for idx, token in enumerate(cmd):
        if token == "-f" and idx + 1 < len(cmd):
            candidate = cmd[idx + 1]
            if candidate.startswith(f"{key}="):
                return candidate.split("=", 1)[1]
    return ""


def _extract_field(cmd: list[str], key: str) -> str | None:
    for idx, token in enumerate(cmd):
        if token == "-F" and idx + 1 < len(cmd):
            candidate = cmd[idx + 1]
            if candidate.startswith(f"{key}="):
                return candidate.split("=", 1)[1]
    return None


def _extract_field_int(cmd: list[str], key: str) -> int:
    raw = _extract_field(cmd, key)
    if raw is None:
        return 0
    return int(raw)


def _pull_files_payload(path: str) -> list[dict[str, Any]]:
    route, _, query = path.partition("?")
    route_parts = route.split("/")
    pr_number = int(route_parts[4]) if len(route_parts) >= 5 else 77928
    params = {
        key: value
        for key, _, value in (segment.partition("=") for segment in query.split("&") if segment)
        if key and value
    }
    page = int(params.get("page", "1"))
    per_page = int(params.get("per_page", "30"))

    if pr_number == 78255:
        files = [
            {
                "filename": ".gitignore",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "patch": "\n".join(
                    [
                        "@@ -68,2 +68,2 @@ Makefile",
                        "-old_ignore",
                        "+new_ignore",
                    ]
                ),
            },
            {
                "filename": "paddle/phi/api/include/compat/ATen/core/TensorBase.h",
                "status": "modified",
                "additions": 2,
                "deletions": 3,
                "changes": 5,
                "patch": "\n".join(
                    [
                        "@@ -257,3 +257,2 @@ class PADDLE_API TensorBase {",
                        "-  c10::TensorOptions options() const {",
                        "-    return c10::TensorOptions().dtype(dtype()).device(device());",
                        "+  TensorOptions options() const {",
                        "+    return TensorOptions().dtype(dtype()).device(device()).layout(layout());",
                    ]
                ),
            },
            {
                "filename": "paddle/phi/api/include/compat/ATen/core/TensorBody.h",
                "status": "modified",
                "additions": 6,
                "deletions": 0,
                "changes": 6,
                "patch": "\n".join(
                    [
                        "@@ -38,2 +38,3 @@",
                        ' #include "paddle/phi/api/include/api.h"',
                        '+#include "glog/logging.h"',
                        " namespace at {",
                        "@@ -397,2 +398,4 @@ class Tensor : public TensorBase {",
                        "   bool is_pinned() const {",
                        '+    LOG(WARNING) << "deprecated";',
                        "+    return false;",
                        "@@ -822,2 +867,1 @@ class Tensor : public TensorBase {",
                        "   PaddleTensor& _PD_GetInner() { return tensor_; }",
                        "-namespace torch {",
                        "-using at::Tensor;",
                    ]
                ),
            },
        ]
    else:
        files = [
            {
                "filename": "python/test_file.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "patch": "\n".join(
                    [
                        "@@ -20,2 +20,2 @@ def demo():",
                        "-old_api_call()",
                        "+new_api_call()",
                    ]
                ),
            }
        ]

    start = max(0, (page - 1) * per_page)
    end = start + per_page
    return files[start:end]


def _repository_api_payload(path: str) -> dict[str, Any] | None:
    route = path.partition("?")[0]
    parts = route.split("/")
    if len(parts) != 3 or parts[0] != "repos":
        return None
    owner = parts[1]
    name = parts[2]
    return {"full_name": f"{owner}/{name}", "default_branch": "main"}


def _repository_contents_payload(path: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    route = path.partition("?")[0]
    marker = "/contents"
    if marker not in route:
        return None
    repo_route, _, remainder = route.partition(marker)
    raw_repo_path = remainder.removeprefix("/")
    route_parts = repo_route.split("/")
    if len(route_parts) < 3:
        return None
    repo = f"{route_parts[1]}/{route_parts[2]}"

    if repo == "PaddlePaddle/Paddle" and raw_repo_path == "":
        return [
            {"type": "file", "path": "README.md", "name": "README.md"},
            {"type": "file", "path": "CONTRIBUTING_GUIDE.md", "name": "CONTRIBUTING_GUIDE.md"},
            {"type": "file", "path": "AGENTS.md", "name": "AGENTS.md"},
        ]

    if repo == "PaddlePaddle/Paddle" and raw_repo_path == ".github":
        return [
            {
                "type": "file",
                "path": ".github/PULL_REQUEST_TEMPLATE.md",
                "name": "PULL_REQUEST_TEMPLATE.md",
            },
            {"type": "file", "path": ".github/CODEOWNERS", "name": "CODEOWNERS"},
        ]

    if repo == "PaddlePaddle/Paddle" and raw_repo_path == ".github/PULL_REQUEST_TEMPLATE":
        return [
            {
                "type": "file",
                "path": ".github/PULL_REQUEST_TEMPLATE.md",
                "name": "PULL_REQUEST_TEMPLATE.md",
            }
        ]

    if repo == "ShigureLab/gh-llm" and raw_repo_path == ".github/PULL_REQUEST_TEMPLATE.md":
        content = "\n".join(
            [
                "## Motivation",
                "",
                "Template motivation section",
                "",
                "## Checklist",
                "",
                "- [ ] Tests added",
            ]
        )
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

    if repo == "ExampleOrg/mixed-case-template" and raw_repo_path == ".github":
        return [
            {
                "type": "file",
                "path": ".github/Pull_Request_Template.TXT",
                "name": "Pull_Request_Template.TXT",
            }
        ]

    if repo == "ExampleOrg/mixed-case-template" and raw_repo_path == ".github/Pull_Request_Template.TXT":
        content = "## Summary\n\nTemplate from mixed-case txt\n"
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

    if repo == "FQ-Studio-SV/SysAcopio" and raw_repo_path == "":
        return [
            {
                "type": "dir",
                "path": "PULL_REQUEST_TEMPLATE",
                "name": "PULL_REQUEST_TEMPLATE",
            }
        ]

    if repo == "FQ-Studio-SV/SysAcopio" and raw_repo_path == "PULL_REQUEST_TEMPLATE":
        return [
            {
                "type": "file",
                "path": "PULL_REQUEST_TEMPLATE/template.md",
                "name": "template.md",
            }
        ]

    if repo == "FQ-Studio-SV/SysAcopio" and raw_repo_path == "PULL_REQUEST_TEMPLATE/template.md":
        content = "## Summary\n\nRoot directory template body\n"
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

    if repo == "DocsTemplateOrg/docs-template" and raw_repo_path == "docs":
        return [
            {
                "type": "dir",
                "path": "docs/PULL_REQUEST_TEMPLATE",
                "name": "PULL_REQUEST_TEMPLATE",
            }
        ]

    if repo == "DocsTemplateOrg/docs-template" and raw_repo_path == "docs/PULL_REQUEST_TEMPLATE":
        return [
            {
                "type": "file",
                "path": "docs/PULL_REQUEST_TEMPLATE/bugfix.md",
                "name": "bugfix.md",
            }
        ]

    if repo == "DocsTemplateOrg/docs-template" and raw_repo_path == "docs/PULL_REQUEST_TEMPLATE/bugfix.md":
        content = "## Summary\n\nDocs directory template body\n"
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

    return None


class _FakeStdin:
    def __init__(self, content: str) -> None:
        self._content = content

    def read(self) -> str:
        return self._content


def _forward_page_payload(page_size: int, after: str | None) -> dict[str, Any]:
    events = _events()
    total_pages = math.ceil(len(events) / TEST_BASE_PAGE_SIZE)
    page_map = {None: 1, **{f"cursor-{idx}": idx + 1 for idx in range(1, total_pages)}}
    page = page_map[after]
    start, canonical_end = _page_bounds(page=page, total_count=len(events), base_page_size=TEST_BASE_PAGE_SIZE)
    end = min(start + page_size, canonical_end)
    chunk = events[start:end]

    end_cursor = f"cursor-{page}" if page < total_pages else None
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "timelineItems": {
                        "totalCount": len(events),
                        "pageInfo": {
                            "hasNextPage": page < total_pages,
                            "hasPreviousPage": page > 1,
                            "startCursor": f"back-{page - 1}" if page > 1 else None,
                            "endCursor": end_cursor,
                        },
                        "nodes": chunk,
                    }
                }
            }
        }
    }


def _backward_page_payload(page_size: int, before: str | None) -> dict[str, Any]:
    events = _events()
    total_pages = math.ceil(len(events) / TEST_BASE_PAGE_SIZE)
    page_map = {None: total_pages, **{f"back-{idx}": idx for idx in range(1, total_pages)}}
    page = page_map[before]
    start, canonical_end = _page_bounds(page=page, total_count=len(events), base_page_size=TEST_BASE_PAGE_SIZE)
    end = min(start + page_size, canonical_end)
    chunk = events[start:end]

    start_cursor = f"back-{page - 1}" if page > 1 else None
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "timelineItems": {
                        "totalCount": len(events),
                        "pageInfo": {
                            "hasNextPage": page < total_pages,
                            "hasPreviousPage": page > 1,
                            "startCursor": start_cursor,
                            "endCursor": f"cursor-{page}" if page < total_pages else None,
                        },
                        "nodes": chunk,
                    }
                }
            }
        }
    }


def _issue_forward_page_payload(page_size: int, after: str | None) -> dict[str, Any]:
    events = _issue_events()
    total_pages = math.ceil(len(events) / TEST_BASE_PAGE_SIZE)
    page_map = {None: 1, **{f"cursor-{idx}": idx + 1 for idx in range(1, total_pages)}}
    page = page_map[after]
    start, canonical_end = _page_bounds(page=page, total_count=len(events), base_page_size=TEST_BASE_PAGE_SIZE)
    end = min(start + page_size, canonical_end)
    chunk = events[start:end]

    end_cursor = f"cursor-{page}" if page < total_pages else None
    return {
        "data": {
            "repository": {
                "issue": {
                    "timelineItems": {
                        "totalCount": len(events),
                        "pageInfo": {
                            "hasNextPage": page < total_pages,
                            "hasPreviousPage": page > 1,
                            "startCursor": f"back-{page - 1}" if page > 1 else None,
                            "endCursor": end_cursor,
                        },
                        "nodes": chunk,
                    }
                }
            }
        }
    }


def _issue_backward_page_payload(page_size: int, before: str | None) -> dict[str, Any]:
    events = _issue_events()
    total_pages = math.ceil(len(events) / TEST_BASE_PAGE_SIZE)
    page_map = {None: total_pages, **{f"back-{idx}": idx for idx in range(1, total_pages)}}
    page = page_map[before]
    start, canonical_end = _page_bounds(page=page, total_count=len(events), base_page_size=TEST_BASE_PAGE_SIZE)
    end = min(start + page_size, canonical_end)
    chunk = events[start:end]

    start_cursor = f"back-{page - 1}" if page > 1 else None
    return {
        "data": {
            "repository": {
                "issue": {
                    "timelineItems": {
                        "totalCount": len(events),
                        "pageInfo": {
                            "hasNextPage": page < total_pages,
                            "hasPreviousPage": page > 1,
                            "startCursor": start_cursor,
                            "endCursor": f"cursor-{page}" if page < total_pages else None,
                        },
                        "nodes": chunk,
                    }
                }
            }
        }
    }


def _review_threads_payload(after: str | None) -> dict[str, Any]:
    if after is not None:
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        }
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "PRRT_mock_2",
                                "isResolved": True,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "rc2",
                                            "path": "python/test_file.py",
                                            "body": "The error message could be more helpful.\n```suggestion\nnew_api_call()\n```",
                                            "line": 22,
                                            "originalLine": 22,
                                            "startLine": None,
                                            "originalStartLine": None,
                                            "diffHunk": "@@ -22,1 +22,1 @@\n-old_api_call()",
                                            "createdAt": "2026-02-14T14:50:02Z",
                                            "outdated": False,
                                            "isMinimized": False,
                                            "minimizedReason": None,
                                            "author": {"login": "reviewer"},
                                            "reactionGroups": [],
                                            "pullRequestReview": {"id": "PRR_mock"},
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "PRRT_mock_1",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "rc1",
                                            "path": "python/test_file.py",
                                            "body": "use clear variable names",
                                            "line": 21,
                                            "originalLine": 21,
                                            "startLine": None,
                                            "originalStartLine": None,
                                            "diffHunk": "@@ -20,2 +20,2 @@\n-old_name\n+new_name",
                                            "createdAt": "2026-02-14T14:50:01Z",
                                            "outdated": False,
                                            "isMinimized": False,
                                            "minimizedReason": None,
                                            "author": {"login": "reviewer"},
                                            "reactionGroups": [{"content": "HEART", "users": {"totalCount": 1}}],
                                            "pullRequestReview": {"id": "PRR_mock"},
                                        },
                                        {
                                            "id": "PRRC_self_1",
                                            "path": "python/test_file.py",
                                            "body": "self reply",
                                            "line": 23,
                                            "originalLine": 23,
                                            "startLine": None,
                                            "originalStartLine": None,
                                            "diffHunk": "@@ -23,1 +23,1 @@\n-old\n+new",
                                            "createdAt": "2026-02-14T14:50:03Z",
                                            "outdated": True,
                                            "isMinimized": False,
                                            "minimizedReason": None,
                                            "author": {"login": "ShigureNyako"},
                                            "reactionGroups": [],
                                            "pullRequestReview": {"id": "PRR_mock"},
                                        },
                                    ]
                                },
                            },
                        ],
                    }
                }
            }
        }
    }


def _checks_payload() -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "nodes": [
                                                {
                                                    "__typename": "CheckRun",
                                                    "name": "lint",
                                                    "status": "COMPLETED",
                                                    "conclusion": "SUCCESS",
                                                    "detailsUrl": "https://github.com/PaddlePaddle/Paddle/actions/runs/101",
                                                    "databaseId": 101,
                                                },
                                                {
                                                    "__typename": "CheckRun",
                                                    "name": "unit-tests",
                                                    "status": "IN_PROGRESS",
                                                    "conclusion": None,
                                                    "detailsUrl": "https://github.com/PaddlePaddle/Paddle/actions/runs/202/job/303",
                                                    "databaseId": 102,
                                                },
                                                {
                                                    "__typename": "StatusContext",
                                                    "context": "cla/check",
                                                    "state": "SUCCESS",
                                                    "targetUrl": "https://example.com/cla",
                                                    "description": "CLA check",
                                                },
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
    }


def _base_events() -> list[dict[str, Any]]:
    long_comment = ("LONG_TEXT " * 220) + "END_MARKER"
    return [
        {
            "__typename": "IssueComment",
            "id": "c1",
            "url": "https://example.com/c1",
            "createdAt": "2026-02-14T14:31:36Z",
            "body": long_comment,
            "author": {"login": "bot"},
            "reactionGroups": [{"content": "THUMBS_UP", "users": {"totalCount": 2}}],
        },
        {
            "__typename": "PullRequestCommit",
            "commit": {
                "oid": "oid-1",
                "committedDate": "2026-02-14T14:31:11Z",
                "messageHeadline": "commit 1",
                "authors": {"nodes": [{"name": "A", "user": {"login": "a"}}]},
            },
        },
        {
            "__typename": "PullRequestCommit",
            "commit": {
                "oid": "oid-2",
                "committedDate": "2026-02-14T14:43:46Z",
                "messageHeadline": "commit 2",
                "authors": {"nodes": [{"name": "B", "user": {"login": "b"}}]},
            },
        },
        {
            "__typename": "IssueComment",
            "id": "c2",
            "url": "https://example.com/c2",
            "createdAt": "2026-02-14T14:44:36Z",
            "body": "comment 2",
            "author": {"login": "user2"},
            "reactionGroups": [],
        },
        {
            "__typename": "PullRequestReview",
            "id": "PRR_mock",
            "submittedAt": "2026-02-14T14:51:00Z",
            "state": "APPROVED",
            "body": "lgtm",
            "isMinimized": True,
            "minimizedReason": "OUTDATED",
            "author": {"login": "reviewer"},
        },
        {
            "__typename": "MergedEvent",
            "id": "m1",
            "createdAt": "2026-02-14T15:10:00Z",
            "actor": {"login": "merger"},
        },
        {
            "__typename": "IssueComment",
            "id": "c3",
            "url": "https://example.com/c3",
            "createdAt": "2026-02-14T15:11:00Z",
            "body": "self comment",
            "author": {"login": "ShigureNyako"},
            "reactionGroups": [],
        },
    ]


def _events_with_web_like_extras() -> list[dict[str, Any]]:
    base = _base_events()
    return [
        *base,
        {
            "__typename": "ReferencedEvent",
            "id": "r1",
            "createdAt": "2026-02-14T15:12:00Z",
            "actor": {"login": "other"},
            "isCrossRepository": False,
            "subject": {
                "__typename": "PullRequest",
                "number": 77887,
                "title": "Test referenced PR",
                "author": {"login": "alice", "name": "Alice"},
                "repository": {"nameWithOwner": "PaddlePaddle/Paddle"},
            },
        },
        {
            "__typename": "CrossReferencedEvent",
            "id": "cr1",
            "createdAt": "2026-02-14T15:12:45Z",
            "actor": {"login": "triager", "name": "Tri Ager"},
            "isCrossRepository": False,
            "source": {
                "__typename": "Issue",
                "number": 12345,
                "title": "Test issue",
                "author": {"login": "bob", "name": "Bob"},
                "repository": {"nameWithOwner": "PaddlePaddle/Paddle"},
            },
        },
        {
            "__typename": "LabeledEvent",
            "id": "l1",
            "createdAt": "2026-02-14T15:13:00Z",
            "actor": {"login": "triager"},
            "label": {"name": "kind/docs"},
        },
        {
            "__typename": "UnlabeledEvent",
            "id": "u1",
            "createdAt": "2026-02-14T15:14:00Z",
            "actor": {"login": "triager"},
            "label": {"name": "needs-review"},
        },
        {
            "__typename": "HeadRefForcePushedEvent",
            "id": "f1",
            "createdAt": "2026-02-14T15:15:00Z",
            "actor": {"login": "author1"},
            "ref": {"name": "feature-branch"},
            "beforeCommit": {"oid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            "afterCommit": {"oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
        },
        {
            "__typename": "PullRequestCommit",
            "commit": {
                "oid": "oid-3",
                "committedDate": "2026-02-14T15:15:10Z",
                "messageHeadline": "commit 3",
                "authors": {"nodes": [{"name": "Author One", "user": {"login": "author1"}}]},
            },
        },
        {
            "__typename": "ReviewDismissedEvent",
            "id": "rd1",
            "createdAt": "2026-02-14T15:15:20Z",
            "dismissalMessage": None,
            "actor": {"login": "reviewer"},
            "review": {
                "author": {"login": "reviewer"},
                "submittedAt": "2026-02-14T14:51:00Z",
            },
        },
        {
            "__typename": "RenamedTitleEvent",
            "id": "rt1",
            "createdAt": "2026-02-14T15:16:00Z",
            "actor": {"login": "author1"},
            "previousTitle": "Old title",
            "currentTitle": "New title",
        },
    ]


def _events() -> list[dict[str, Any]]:
    return _base_events()


def test_display_command_env_is_used_in_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_LLM_DISPLAY_CMD", "gh llm")
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--page-size", "2"])
    assert code == 0

    out = capsys.readouterr().out
    assert "gh llm pr timeline-expand 2 --pr 77928 --repo PaddlePaddle/Paddle" in out


def test_pr_timeline_expand_with_expand_option(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "pr",
            "timeline-expand",
            "3",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--expand",
            "resolved,minimized",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Review comments (3/3 shown):" in out
    assert "resolved review comments are collapsed" not in out
    assert "hidden review comments are collapsed" not in out


def test_pr_thread_expand_by_thread_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "pr",
            "thread-expand",
            "PRRT_mock_1",
            "--pr",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "## Review Thread PRRT_mock_1" in out
    assert "review_id: PRR_mock" in out
    assert "Thread[1] PRRT_mock_1" in out
    assert "self reply" in out


def test_issue_timeline_expand_with_expand_minimized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "issue",
            "timeline-expand",
            "1",
            "--issue",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--expand",
            "minimized",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "(comment hidden: outdated)" not in out


def test_issue_timeline_expand_with_before_and_expand_minimized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "issue",
            "timeline-expand",
            "1",
            "--issue",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--before",
            "2026-02-13T13:30:00Z",
            "--expand",
            "minimized",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "timeline_before: 2026-02-13T13:30:00Z" in out
    assert "### Page 1/2" in out
    assert "(comment hidden: outdated)" not in out
    assert "cross-reference by @alice (Alice)" in out


def test_issue_details_expand_with_before_uses_filtered_expanded_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def issue_events_with_details() -> list[dict[str, Any]]:
        return [
            {
                "__typename": "IssueComment",
                "id": "ic0",
                "url": "https://example.com/ic0",
                "createdAt": "2026-02-13T10:00:00Z",
                "body": "older issue comment",
                "isMinimized": False,
                "minimizedReason": None,
                "author": {"login": "alice"},
                "reactionGroups": [],
            },
            {
                "__typename": "IssueComment",
                "id": "ic_details",
                "url": "https://example.com/ic-details",
                "createdAt": "2026-02-13T11:00:00Z",
                "body": "intro\n<details><summary>why</summary>\nfiltered details body\n</details>",
                "isMinimized": True,
                "minimizedReason": "OUTDATED",
                "author": {"login": "bot"},
                "reactionGroups": [],
            },
            {
                "__typename": "IssueComment",
                "id": "ic2",
                "url": "https://example.com/ic2",
                "createdAt": "2026-02-13T13:00:00Z",
                "body": "later issue comment",
                "isMinimized": False,
                "minimizedReason": None,
                "author": {"login": "ShigureNyako"},
                "reactionGroups": [],
            },
            {
                "__typename": "ClosedEvent",
                "id": "iclose1",
                "createdAt": "2026-02-13T14:00:00Z",
                "actor": {"login": "ShigureNyako"},
            },
        ]

    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)
    monkeypatch.setattr(sys.modules[__name__], "_issue_events", issue_events_with_details)

    code = cli.run(
        [
            "issue",
            "details-expand",
            "2",
            "--issue",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--before",
            "2026-02-13T13:30:00Z",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "## Details Blocks for Event 2" in out
    assert "<summary>why</summary>" in out
    assert "filtered details body" in out
    assert "(details body collapsed)" not in out


def test_pr_view_show_timeline_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "pr",
            "view",
            "77928",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--show",
            "timeline",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "## Timeline" in out
    assert "### Page 1/4" in out
    assert "## Actions" not in out
    assert "## Description" not in out


def test_issue_view_show_summary_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(
        [
            "issue",
            "view",
            "77924",
            "--repo",
            "PaddlePaddle/Paddle",
            "--page-size",
            "2",
            "--show",
            "meta,description",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "## Description" in out
    assert "## Timeline" not in out
    assert "## Actions" not in out


def test_pr_actions_for_closed_unmerged_show_reopen_and_branch_delete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77827", "--repo", "PaddlePaddle/Paddle", "--show", "actions"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Reopen PR via gh: `gh pr reopen 77827 --repo PaddlePaddle/Paddle`" in out
    assert "Close PR via gh:" not in out
    assert (
        "Delete head branch via gh: `gh api -X DELETE repos/PaddlePaddle/Paddle/git/refs/heads/feature/keep-branch`"
        in out
    )
    assert "Restore head branch via gh:" not in out


def test_pr_actions_for_closed_merged_show_branch_restore_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77960", "--repo", "PaddlePaddle/Paddle", "--show", "actions"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Close PR via gh:" not in out
    assert "Reopen PR via gh:" not in out
    assert "Delete head branch via gh:" not in out
    assert "Restore head branch via gh:" in out
    assert "gh api repos/PaddlePaddle/Paddle/git/refs -X POST" in out


def test_pr_mergeability_show_conflict_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77971", "--repo", "PaddlePaddle/Paddle", "--show", "mergeability"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Mergeability" in out
    assert "Status: Merging is blocked" in out
    assert "Merge conflicts detected." in out
    assert "pr conflict-files --pr 77971 --repo PaddlePaddle/Paddle" in out


def test_pr_mergeability_for_merged_pr_shows_already_merged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77960", "--repo", "PaddlePaddle/Paddle", "--show", "mergeability"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Mergeability" in out
    assert "Status: Already merged" in out
    assert "Merge actions:" not in out


def test_pr_conflict_files_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    def _mock_detect_conflict_files(
        self: github_api.GitHubClient,
        *,
        base_repo: str,
        base_ref_name: str | None,
        base_ref_oid: str | None,
        head_repo: str,
        head_ref_name: str | None,
        head_ref_oid: str | None,
    ) -> tuple[str, ...]:
        del self, base_repo, base_ref_name, base_ref_oid, head_repo, head_ref_name, head_ref_oid
        return ("python/a.py", "python/b.py")

    monkeypatch.setattr(github_api.GitHubClient, "_detect_conflict_files", _mock_detect_conflict_files)
    code = cli.run(["pr", "conflict-files", "--pr", "77971", "--repo", "PaddlePaddle/Paddle"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Conflict Files" in out
    assert "- `python/a.py`" in out
    assert "- `python/b.py`" in out


def test_pr_mergeability_show_merge_actions_with_repo_method_filter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77972", "--repo", "PaddlePaddle/Paddle", "--show", "mergeability"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Mergeability" in out
    assert "Status: Merging is allowed" in out
    assert "⌨ merge_subject: 'Timeline test (#77972)'" in out
    assert "⌨ merge_body (default):" in out
    assert "   <optional_merge_body>" in out
    assert "Co-authored-by: Alice Example <alice@example.com>" in out
    assert "Co-authored-by: Bob Example <bob@example.com>" in out
    assert (
        "⏎ merge via gh: `gh pr merge 77972 --repo PaddlePaddle/Paddle --merge --subject 'Timeline test (#77972)' --body '<merge_body>'`"
        in out
    )
    assert (
        "⏎ squash via gh: `gh pr merge 77972 --repo PaddlePaddle/Paddle --squash --subject 'Timeline test (#77972)' --body '<merge_body>'`"
        in out
    )
    assert "⏎ rebase via gh: `gh pr merge 77972 --repo PaddlePaddle/Paddle --rebase`" not in out
    assert "Disabled by repository settings: rebase" in out


def test_pr_mergeability_rebase_action_has_no_subject_or_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_api.subprocess, "run", GhResponder().run)

    code = cli.run(["pr", "view", "77928", "--repo", "PaddlePaddle/Paddle", "--show", "mergeability"])
    assert code == 0
    out = capsys.readouterr().out
    assert "⏎ rebase via gh: `gh pr merge 77928 --repo PaddlePaddle/Paddle --rebase`" in out
    assert "--rebase --subject" not in out
    assert "--rebase --body" not in out


def test_pr_view_invalid_expand_error_lists_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.run(["pr", "view", "77960", "--repo", "PaddlePaddle/Paddle", "--expand", "sss"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error: unknown expand option: sss." in err
    assert "Valid values: resolved, minimized, details, all." in err


def test_pr_view_hidden_expand_alias_removed(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.run(["pr", "view", "77960", "--repo", "PaddlePaddle/Paddle", "--expand", "hidden"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error: unknown expand option: hidden." in err


def test_pr_view_outdated_expand_option_removed(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.run(["pr", "view", "77960", "--repo", "PaddlePaddle/Paddle", "--expand", "outdated"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error: unknown expand option: outdated." in err
    assert "Valid values: resolved, minimized, details, all." in err


def test_issue_view_invalid_show_error_lists_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.run(["issue", "view", "77924", "--repo", "PaddlePaddle/Paddle", "--show", "abc"])
    assert code == 1
    err = capsys.readouterr().err
    assert "error: unknown show option: abc." in err
    assert "Valid values: meta, description, timeline, actions, summary, all." in err


TEST_BASE_PAGE_SIZE = 2


def _issue_events() -> list[dict[str, Any]]:
    long_comment = ("ISSUE_LONG_TEXT " * 220) + "ISSUE_END_MARKER"
    return [
        {
            "__typename": "IssueComment",
            "id": "ic1",
            "url": "https://example.com/ic1",
            "createdAt": "2026-02-13T10:00:00Z",
            "body": long_comment,
            "isMinimized": True,
            "minimizedReason": "OUTDATED",
            "author": {"login": "bot"},
            "reactionGroups": [{"content": "THUMBS_UP", "users": {"totalCount": 1}}],
        },
        {
            "__typename": "CrossReferencedEvent",
            "id": "icr1",
            "createdAt": "2026-02-13T12:00:00Z",
            "actor": {"login": "alice", "name": "Alice"},
            "isCrossRepository": False,
            "source": {
                "__typename": "PullRequest",
                "number": 77900,
                "title": "Related PR",
                "author": {"login": "bob", "name": "Bob"},
                "repository": {"nameWithOwner": "PaddlePaddle/Paddle"},
            },
        },
        {
            "__typename": "IssueComment",
            "id": "ic2",
            "url": "https://example.com/ic2",
            "createdAt": "2026-02-13T13:00:00Z",
            "body": "self issue comment",
            "isMinimized": False,
            "minimizedReason": None,
            "author": {"login": "ShigureNyako"},
            "reactionGroups": [],
        },
        {
            "__typename": "ClosedEvent",
            "id": "iclose1",
            "createdAt": "2026-02-13T14:00:00Z",
            "actor": {"login": "ShigureNyako"},
        },
        {
            "__typename": "MarkedAsDuplicateEvent",
            "id": "made1",
            "createdAt": "2026-02-13T14:10:00Z",
            "actor": {"login": "SigureMo", "name": "Nyakku Shigure"},
            "isCrossRepository": False,
            "canonical": {
                "__typename": "Issue",
                "number": 77924,
                "title": "Issue timeline test",
                "author": {"login": "ShigureNyako"},
                "repository": {"nameWithOwner": "PaddlePaddle/Paddle"},
            },
            "duplicate": {
                "__typename": "Issue",
                "number": 77925,
                "title": "Duplicate issue",
                "author": {"login": "alice", "name": "Alice"},
                "repository": {"nameWithOwner": "PaddlePaddle/Paddle"},
            },
        },
    ]


def _page_bounds(page: int, total_count: int, base_page_size: int) -> tuple[int, int]:
    start = (page - 1) * base_page_size
    end = min(start + base_page_size, total_count)
    return start, end
