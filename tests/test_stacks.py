import json
from dataclasses import replace

import pytest

from gh_llm import cli, github_api as api
from gh_llm.diagnostics import GhCommandError
from gh_llm.models import (
    CheckItem,
    CheckResults,
    PullRequestMeta,
    PullRequestRef,
    PullRequestStack,
    StackEntry,
    TimelineContext,
)
from gh_llm.pager import build_context_from_meta
from gh_llm.render import render_mergeability_section
from gh_llm.stack_render import render_stack_section

REF = PullRequestRef("owner", "repo", 20)
HEAD = "a" * 40


def meta(*, position: int = 2, size: int = 3) -> PullRequestMeta:
    return PullRequestMeta(
        ref=replace(REF, number=position * 10),
        title="Middle layer",
        url="https://github.com/owner/repo/pull/20",
        author="alice",
        state="OPEN",
        is_draft=False,
        body="",
        updated_at="",
        head_ref_oid=HEAD,
        head_ref_name="layer-2",
        base_ref_name="layer-1",
        squash_merge_allowed=True,
        stack=PullRequestStack(number=50, position=position, size=size, base_ref_name="main"),
    )


def entry(position: int, **changes: object) -> StackEntry:
    value = StackEntry(
        position,
        position * 10,
        f"Layer {position}",
        "OPEN",
        head_ref_oid=HEAD,
        merge_state_status="CLEAN",
        mergeable="MERGEABLE",
        checks_state="SUCCESS",
    )
    return replace(value, **changes)  # type: ignore[invalid-argument-type]


def context(entries: tuple[StackEntry, ...], **changes: object) -> TimelineContext:
    value = meta()
    assert value.stack is not None
    value = replace(value, stack=replace(value.stack, entries=entries), **changes)  # type: ignore[invalid-argument-type]
    return build_context_from_meta(meta=value, page_size=10)


def test_stack_context_round_trip_and_merged_deleted_refs() -> None:
    value = context(
        tuple(entry(i, state="MERGED", head_ref_name=None) for i in range(1, 4)),
        state="MERGED",
        is_merged=True,
        head_ref_deleted=True,
    )
    restored = TimelineContext.from_dict(json.loads(json.dumps(value.to_dict())))
    assert restored.stack == value.stack
    assert restored.stack is not None and restored.stack.complete
    rendered = "\n".join(render_stack_section(restored))
    assert "layer 2/3" in rendered and "#20 [MERGED]" in rendered and "← current" in rendered
    merge = "\n".join(render_mergeability_section(context=restored, checks=[]))
    assert "Already merged" in merge and "gh pr merge" not in merge


def test_merge_scope_skips_merged_layers_and_excludes_upper_blockers() -> None:
    value = context((entry(1, state="MERGED"), entry(2), entry(3, is_draft=True, merge_state_status="BLOCKED")))
    rendered = "\n".join(render_mergeability_section(context=value, checks=[]))
    assert "scope into `main`: #20\n" in rendered
    assert "No blockers reported" in rendered
    assert "#30:" not in rendered and "gh pr merge" not in rendered
    assert f"gh-llm pr merge 20 --repo owner/repo --squash --head {HEAD}" in rendered
    assert "--web" not in rendered and "--subject" not in rendered


def test_downstack_draft_review_and_required_checks_block_merge() -> None:
    value = context((entry(1, is_draft=True, review_decision="REVIEW_REQUIRED"), entry(2), entry(3)))
    checks = [CheckItem("test", "required-context", "EXPECTED", False, required=True)]
    rendered = "\n".join(render_mergeability_section(context=value, checks=checks))
    assert "Stack merge is blocked" in rendered
    assert "#10: draft, review: REVIEW_REQUIRED" in rendered
    assert "#20: required checks not passed: test" in rendered
    assert "gh-llm pr merge" not in rendered and "--web" not in rendered


@pytest.mark.parametrize(
    "entries",
    [
        (entry(1), entry(2)),
        (entry(1), entry(2, merge_state_status="UNKNOWN"), entry(3)),
    ],
)
def test_incomplete_or_unknown_stack_cannot_claim_ready(entries: tuple[StackEntry, ...]) -> None:
    rendered = "\n".join(render_mergeability_section(context=context(entries), checks=[]))
    assert "readiness is unknown" in rendered and "Merging is allowed" not in rendered
    assert "gh-llm pr merge" not in rendered and "--web" not in rendered


def test_optional_check_failure_does_not_block_stack() -> None:
    checks = [CheckItem("optional", "check-run", "COMPLETED/FAILURE", False, required=False)]
    value = context(tuple(entry(i, checks_state="FAILURE") for i in range(1, 4)))
    assert "No blockers reported" in "\n".join(render_mergeability_section(context=value, checks=checks))


def stack_page(value: PullRequestMeta, positions: range, *, cursor: str | None = None) -> dict[str, object]:
    assert value.stack is not None
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": HEAD,
                    "stackEntry": {"position": value.stack.position},
                    "stack": {
                        "number": 50,
                        "size": value.stack.size,
                        "baseRefName": "main",
                        "entries": {
                            "nodes": [
                                {
                                    "position": i,
                                    "pullRequest": {
                                        "number": i * 10,
                                        "title": f"Layer {i}",
                                        "state": "OPEN",
                                        "isDraft": True,
                                        "headRefOid": HEAD,
                                        "mergeStateStatus": "BLOCKED",
                                        "mergeable": "MERGEABLE",
                                        "reviewDecision": "REVIEW_REQUIRED",
                                        "statusCheckRollup": {"state": "PENDING"},
                                    },
                                }
                                for i in positions
                            ],
                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        },
                    },
                }
            }
        }
    }


def graphql_stub(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, object]]) -> list[dict[str, str | int]]:
    calls: list[dict[str, str | int]] = []

    def fetch(query: str, variables: dict[str, str | int]) -> dict[str, object]:
        del query
        calls.append(variables)
        return pages.pop(0)

    monkeypatch.setattr(api, "_run_graphql_payload", fetch)
    return calls


def test_long_stack_pagination_and_draft_members(monkeypatch: pytest.MonkeyPatch) -> None:
    value = meta(size=101)
    calls = graphql_stub(
        monkeypatch, [stack_page(value, range(1, 101), cursor="next"), stack_page(value, range(101, 102))]
    )
    loaded = api.GitHubClient().fetch_pull_request_stack(value)
    assert loaded.stack is not None and loaded.stack.complete and len(loaded.stack.entries) == 101
    assert loaded.stack.entries[-1].is_draft
    assert calls[1]["after"] == "next"


def test_stack_changed_while_loading_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    graphql_stub(monkeypatch, [stack_page(meta(size=4), range(1, 5))])
    with pytest.raises(RuntimeError, match="membership changed"):
        api.GitHubClient().fetch_pull_request_stack(meta())


@pytest.mark.parametrize(
    "message,fallback",
    [
        ("Field 'stack' doesn't exist on type 'PullRequest'", True),
        ("Resource not accessible by integration", False),
        ('Post "https://api.github.com/graphql": EOF', False),
    ],
)
def test_only_missing_stack_schema_falls_back(monkeypatch: pytest.MonkeyPatch, message: str, fallback: bool) -> None:
    calls: list[str] = []

    def fetch(query: str, variables: dict[str, str | int]) -> dict[str, object]:
        del variables
        calls.append(query)
        if len(calls) == 1:
            raise GhCommandError(cmd=["gh", "api", "graphql"], stderr=message)
        return {"data": {"repository": {"pullRequest": {"id": "PR"}}}}

    monkeypatch.setattr(api, "_run_graphql_payload", fetch)
    client = api.GitHubClient()
    if fallback:
        result = client._fetch_pull_request_actions_meta(REF)
        assert result["stack_supported"] is False
        assert api.STACK_MEMBERSHIP_FIELDS not in calls[1]
    else:
        with pytest.raises(GhCommandError):
            client._fetch_pull_request_actions_meta(REF)
        assert len(calls) == 1


def check_node(
    name: str, *, suite: int = 2, workflow: int = 1, app: int = 15368, passed: bool = True
) -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": "SUCCESS" if passed else "FAILURE",
        "isRequired": False,
        "databaseId": suite * 10,
        "checkSuite": {
            "databaseId": suite,
            "createdAt": f"2026-09-30T00:00:0{suite}Z",
            "app": {"databaseId": app},
            "workflowRun": {
                "databaseId": suite,
                "event": "pull_request",
                "workflow": {"databaseId": workflow, "name": f"Workflow {workflow}"},
            },
        },
    }


def checks_page(nodes: list[dict[str, object]], *, cursor: str | None = None, head: str = HEAD) -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": head,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": head,
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "nodes": nodes,
                                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                                        }
                                    },
                                }
                            }
                        ]
                    },
                }
            }
        }
    }


def test_checks_paginate_keep_distinct_workflows_and_only_latest_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = graphql_stub(
        monkeypatch,
        [
            checks_page([check_node(f"test-{i}", suite=1, passed=False) for i in range(100)], cursor="next"),
            checks_page([check_node("test-0"), check_node("test-0", workflow=2, passed=False)]),
        ],
    )
    results = api.GitHubClient().fetch_checks(REF)
    assert len(results.items) == 2
    assert [item.workflow for item in results.items] == ["Workflow 1", "Workflow 2"]
    assert [item.passed for item in results.items] == [True, False]
    assert calls[1]["after"] == "next" and results.head_oid == HEAD


def test_checks_head_changed_between_pages_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    graphql_stub(monkeypatch, [checks_page([], cursor="next"), checks_page([], head="b" * 40)])
    with pytest.raises(RuntimeError, match="head changed"):
        api.GitHubClient().fetch_checks(REF)


def test_stack_required_checks_use_trunk_and_app_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    graphql_stub(monkeypatch, [checks_page([check_node("unit", app=999), check_node("lint"), check_node("extra")])])
    paths: list[str] = []

    def branch(cmd: list[str]) -> dict[str, object]:
        paths.append(cmd[2])
        return {
            "protection": {
                "required_status_checks": {"checks": [{"context": "unit", "app_id": 15368}], "contexts": ["unit"]}
            }
        }

    def rules(cmd: list[str]) -> object:
        paths.append(cmd[2])
        return [
            [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "lint", "integration_id": 15368}]},
                }
            ]
        ]

    monkeypatch.setattr(api, "_run_command_json", branch)
    monkeypatch.setattr(api, "_run_command_json_any", rules)
    results = api.GitHubClient().fetch_checks(REF, meta=meta())
    assert results.requirements_known and results.required_base_ref == "main"
    assert all("/main" in path for path in paths)
    expected = [item for item in results.items if item.status == "EXPECTED"]
    assert len(expected) == 1 and expected[0].name == "unit" and expected[0].app_id == 15368
    assert [item.required for item in results.items[:3]] == [False, True, False]


def test_requirement_access_failure_is_not_an_empty_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    graphql_stub(monkeypatch, [checks_page([])])

    def denied(cmd: list[str]) -> object:
        raise GhCommandError(cmd=cmd, stderr="HTTP 403: Forbidden")

    monkeypatch.setattr(api, "_run_command_json", denied)
    monkeypatch.setattr(api, "_run_command_json_any", denied)
    results = api.GitHubClient().fetch_checks(REF, meta=meta())
    assert not results.requirements_known and results.required_base_ref == "main"


def test_partial_rules_preserve_known_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    graphql_stub(monkeypatch, [checks_page([check_node("unit", passed=False), check_node("unknown")])])
    monkeypatch.setattr(
        api,
        "_run_command_json",
        lambda _cmd: {
            "protection": {"required_status_checks": {"contexts": ["unit"]}},
        },
    )

    def denied(cmd: list[str]) -> object:
        raise GhCommandError(cmd=cmd, stderr="HTTP 403: Forbidden")

    monkeypatch.setattr(api, "_run_command_json_any", denied)
    results = api.GitHubClient().fetch_checks(REF, meta=meta())
    assert not results.requirements_known
    assert [item.required for item in results.items] == [True, None]
    value = context(tuple(entry(i) for i in range(1, 4)))
    output = "\n".join(render_mergeability_section(context=value, checks=[], requirements_known=False))
    assert "readiness is unknown" in output and "configuration is partially unavailable" in output


def test_stack_only_cli_is_lightweight(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    value = meta(size=11, position=9)
    monkeypatch.setattr(api.GitHubClient, "resolve_pull_request", lambda *_args, **_kwargs: value)
    calls = graphql_stub(monkeypatch, [stack_page(value, range(1, 12))])
    assert cli.run(["pr", "view", "90", "--repo", "owner/repo", "--show", "stack"]) == 0
    output = capsys.readouterr().out
    assert "layer 9/11" in output and "#110 [DRAFT]" in output
    assert len(calls) == 1 and "## Checks" not in output and "## Timeline" not in output


def test_historical_stack_checks_do_not_apply_current_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    graphql_stub(monkeypatch, [checks_page([check_node("historical")])])
    results = api.GitHubClient().fetch_checks(REF, meta=replace(meta(), state="MERGED", is_merged=True))
    assert results.required_base_ref is None
    assert results.items[0].required is None


def test_closed_checks_all_fetches_historical_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = replace(meta(), state="MERGED", is_merged=True, stack=None)
    calls: list[int] = []
    monkeypatch.setattr(api.GitHubClient, "resolve_pull_request", lambda *_args, **_kwargs: value)

    def fetch(*_args: object, **_kwargs: object) -> CheckResults:
        calls.append(1)
        return CheckResults((CheckItem("historical", "check-run", "COMPLETED/SUCCESS", True),), HEAD)

    monkeypatch.setattr(api.GitHubClient, "fetch_checks", fetch)
    assert cli.run(["pr", "checks", "--pr", "20", "--repo", "owner/repo"]) == 0
    assert not calls and "hidden by default" in capsys.readouterr().out
    assert cli.run(["pr", "checks", "--pr", "20", "--repo", "owner/repo", "--all"]) == 0
    assert calls == [1] and "historical" in capsys.readouterr().out


def test_base_change_event_uses_historical_refs() -> None:
    event = api._parse_node(
        {
            "__typename": "BaseRefChangedEvent",
            "id": "base-change",
            "createdAt": "2026-09-30T00:00:00Z",
            "actor": {"login": "alice"},
            "previousRefName": "old-layer",
            "currentRefName": "new-layer",
        },
        ref=REF,
        threads_for_review={},
        timeline_window=None,
        show_resolved_details=False,
        show_outdated_details=False,
        show_minimized_details=False,
        show_details_blocks=False,
        review_threads_window=None,
        diff_hunk_lines=None,
        viewer_login="",
        subject_kind="pr",
    )
    assert event is not None
    assert event.kind == "pr/base-changed"
    assert event.summary == "base changed from `old-layer` to `new-layer`"
    for query in [api.FORWARD_TIMELINE_QUERY, api.BACKWARD_TIMELINE_QUERY]:
        assert "BASE_REF_CHANGED_EVENT" in query and "... on BaseRefChangedEvent" in query


def test_null_stack_member_remains_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    value = meta()
    payload: dict[str, object] = {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": HEAD,
                    "stackEntry": {"position": 2},
                    "stack": {
                        "number": 50,
                        "size": 3,
                        "baseRefName": "main",
                        "entries": {
                            "nodes": [None, {"position": 1, "pullRequest": None}],
                            "pageInfo": {"hasNextPage": False},
                        },
                    },
                }
            }
        }
    }
    graphql_stub(monkeypatch, [payload])
    loaded = api.GitHubClient().fetch_pull_request_stack(value)
    assert loaded.stack is not None and not loaded.stack.complete


def test_schema_unavailable_never_suggests_ordinary_merge() -> None:
    value = context((), stack_supported=False)
    value = replace(value, stack=None)
    output = "\n".join(render_mergeability_section(context=value, checks=[]))
    assert "Unknown" in output and "gh pr merge" not in output
