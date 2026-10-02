import json
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from gh_llm import cli, github_api as api
from gh_llm.commands import pr_merge
from gh_llm.models import PullRequestMeta, PullRequestRef, PullRequestStack, StackEntry

if TYPE_CHECKING:
    from pathlib import Path

REF = PullRequestRef("owner", "repo", 20)
HEAD = "a" * 40
UUID = "630b9d5e-3f2a-4f7e-8b0c-2d5f9a8c1e42"
ENDPOINT = "repos/owner/repo/pulls/20/merge-async"
PENDING: dict[str, object] = {
    "status": "pending",
    "details": {"uuid": UUID, "merge_method": "squash", "merge_action": "default", "expected_head_sha": HEAD},
}
MERGED: dict[str, object] = {"status": "merged", "details": {"message": "Pull request was merged.", "sha": "b" * 40}}


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> PullRequestMeta:
    value = PullRequestMeta(
        REF, "A change", "https://github.com/owner/repo/pull/20", "alice", "OPEN", False, "", "", head_ref_oid=HEAD
    )
    monkeypatch.setattr(api.GitHubClient, "resolve_pull_request", lambda *_args, **_kwargs: value)
    monkeypatch.setattr(api.GitHubClient, "fetch_pull_request_stack", lambda _self, meta: meta)
    monkeypatch.setattr(pr_merge.time, "sleep", lambda _seconds: None)
    return value


def respond(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[dict[str, object] | str, int, str]]
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        payload, code, stderr = responses.pop(0)
        return subprocess.CompletedProcess(
            cmd, code, stdout=payload if isinstance(payload, str) else json.dumps(payload), stderr=stderr
        )

    monkeypatch.setattr(api.subprocess, "run", run)
    return calls


def test_merge_waits_and_preserves_explicit_message(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    body = "Details: `code` and $literal\n\nCo-authored-by: Codex <noreply@openai.com>\n"
    body_file = tmp_path / "body.md"
    body_file.write_text(body)
    calls = respond(monkeypatch, [(PENDING, 0, ""), (MERGED, 0, "")])
    assert (
        cli.run(
            [
                "pr",
                "merge",
                "20",
                "--repo",
                "owner/repo",
                "--squash",
                "--head",
                HEAD,
                "--subject",
                "A 'quoted' title",
                "--body-file",
                str(body_file),
            ]
        )
        == 0
    )
    put, get = calls
    assert put[:3] == ["gh", "api", ENDPOINT]
    assert "PUT" in put and f"sha={HEAD}" in put
    assert "commit_title=A 'quoted' title" in put and f"commit_message={body}" in put
    assert "merge_method=squash" in put and "merge_action=default" in put and "bypass_rules=false" in put
    assert "X-GitHub-Api-Version: 2026-03-10" in put
    assert get[:3] == ["gh", "api", f"{ENDPOINT}/{UUID}"] and "PUT" not in get
    out = capsys.readouterr().out
    assert f"request_id: {UUID}" in out and "status: pending" in out and "status: merged" in out
    assert f"merge_commit: {'b' * 40}" in out and f"pr merge-status {UUID}" in out


@pytest.mark.parametrize(("status", "exit_code"), [("merged", 0), ("enqueued", 0), ("failed", 1)])
def test_immediate_results(
    meta: PullRequestMeta,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
) -> None:
    calls = respond(monkeypatch, [({"status": status, "details": {"message": "API result"}}, 0, "")])
    assert cli.run(["pr", "merge", "20", "--repo", "owner/repo"]) == exit_code
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert f"status: {status}" in out
    if status == "enqueued":
        assert "has not necessarily merged" in out and "status: merged" not in out


def test_existing_request_is_followed_without_applying_new_options(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = respond(monkeypatch, [(PENDING, 1, "gh: request exists (HTTP 409)"), (MERGED, 0, "")])
    assert cli.run(["pr", "merge", "20", "--rebase"]) == 0
    out = capsys.readouterr().out
    assert "existing_request: true" in out and "new merge options were not applied" in out
    assert "merge_method: squash" in out
    assert sum("PUT" in call for call in calls) == 1


@pytest.mark.parametrize("timeout", [0, 3])
def test_pending_timeout_retains_resume_command(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], timeout: int
) -> None:
    times = iter([0, 0, 3])
    monkeypatch.setattr(pr_merge.time, "monotonic", lambda: next(times))
    calls = respond(monkeypatch, [(PENDING, 0, ""), (PENDING, 0, "")])
    assert cli.run(["pr", "merge", "20", "--timeout", str(timeout)]) == 2
    assert len(calls) == (1 if timeout == 0 else 2)
    out = capsys.readouterr().out
    assert f"pr merge-status {UUID} --pr 20 --repo owner/repo --timeout 60" in out
    assert "still pending" in out and "status: merged" not in out


def test_merge_status_only_reads_existing_request(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = respond(monkeypatch, [(PENDING, 0, ""), (MERGED, 0, "")])
    assert cli.run(["pr", "merge-status", UUID, "--pr", "20", "--repo", "owner/repo", "--timeout", "60"]) == 0
    assert len(calls) == 2 and all(call[2] == f"{ENDPOINT}/{UUID}" and "PUT" not in call for call in calls)
    assert "status: merged" in capsys.readouterr().out


@pytest.mark.parametrize("during_poll", [False, True])
def test_network_failure_never_replays_merge(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], during_poll: bool
) -> None:
    responses: list[tuple[dict[str, object] | str, int, str]] = [(PENDING, 0, "")] if during_poll else []
    responses.append(("", 1, "connection reset by peer"))
    calls = respond(monkeypatch, responses)
    assert cli.run(["pr", "merge", "20"]) == 1
    assert sum("PUT" in call for call in calls) == 1
    out = capsys.readouterr().out
    assert "status: merged" not in out
    if during_poll:
        assert f"pr merge-status {UUID}" in out


@pytest.mark.parametrize(
    "changes",
    [
        {"head_ref_oid": "c" * 40},
        {"head_ref_oid": None},
        {"is_draft": True},
        {"state": "CLOSED"},
        {"state": "UNKNOWN"},
        {"stack_supported": False},
    ],
)
def test_unsafe_snapshot_does_not_submit(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    value = replace(meta, **changes)  # type: ignore[invalid-argument-type]
    monkeypatch.setattr(api.GitHubClient, "resolve_pull_request", lambda *_args, **_kwargs: value)
    calls = respond(monkeypatch, [])
    assert cli.run(["pr", "merge", "20", "--head", HEAD]) == 1
    assert calls == []


def test_stack_merge_submits_selected_pr_once(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = (
        StackEntry(1, 10, "Lower", "OPEN"),
        StackEntry(2, 20, "Selected", "OPEN"),
        StackEntry(3, 30, "Upper", "OPEN", is_draft=True),
    )
    value = replace(meta, stack=PullRequestStack(50, 2, 3, "main", entries))
    monkeypatch.setattr(api.GitHubClient, "resolve_pull_request", lambda *_args, **_kwargs: value)
    calls = respond(monkeypatch, [(MERGED, 0, "")])
    assert cli.run(["pr", "merge", "20", "--squash"]) == 0
    out = capsys.readouterr().out
    assert "Stack merge scope into `main`: #10 → #20" in out and "#30" not in out
    assert len(calls) == 1 and calls[0][2] == ENDPOINT


@pytest.mark.parametrize(
    "entries",
    [
        (StackEntry(2, 20, "Selected", "OPEN"),),
        (StackEntry(1, 10, "Lower", "OPEN", is_draft=True), StackEntry(2, 20, "Selected", "OPEN")),
        (StackEntry(1, 10, "Lower", "OPEN"), StackEntry(2, 30, "Other", "OPEN")),
    ],
)
def test_incomplete_or_blocked_stack_does_not_submit(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, entries: tuple[StackEntry, ...]
) -> None:
    value = replace(meta, stack=PullRequestStack(50, 2, 2, "main", entries))
    monkeypatch.setattr(api.GitHubClient, "resolve_pull_request", lambda *_args, **_kwargs: value)
    calls = respond(monkeypatch, [])
    assert cli.run(["pr", "merge", "20"]) == 1
    assert calls == []


def test_queue_and_explicit_bypass_are_sent(meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = respond(monkeypatch, [({"status": "enqueued", "details": {}}, 0, "")])
    assert cli.run(["pr", "merge", "20", "--merge-action", "merge_queue", "--bypass-rules"]) == 0
    assert "merge_action=merge_queue" in calls[0] and "bypass_rules=true" in calls[0]
    assert not any(field.startswith("merge_method=") for field in calls[0])


@pytest.mark.parametrize(
    "args",
    [
        ["merge", "20", "--timeout", "-1"],
        ["merge", "20", "--merge-action", "merge_queue", "--squash"],
        ["merge-status", "../invalid", "--pr", "20"],
    ],
)
def test_invalid_arguments_do_not_call_api(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    calls = respond(monkeypatch, [])
    assert cli.run(["pr", *args]) == 1
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "pending", "details": {}},
        {"status": "unknown", "details": {}},
        {"status": "pending", "details": {"uuid": "not-a-uuid"}},
    ],
)
def test_malformed_result_is_not_success(
    meta: PullRequestMeta, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    respond(monkeypatch, [(payload, 0, "")])
    assert cli.run(["pr", "merge", "20"]) == 1
