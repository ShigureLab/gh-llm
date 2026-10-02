import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gh_llm.commands.options import add_body_input_arguments, resolve_file_or_inline_text, resolve_subject
from gh_llm.github_api import GitHubClient
from gh_llm.invocation import display_command_with

if TYPE_CHECKING:
    from gh_llm.models import AsyncMergeResult, PullRequestRef


def register_merge_parsers(subparsers: Any) -> None:
    parser = subparsers.add_parser("merge", help="merge a PR and its open downstack PRs through the async merge API")
    parser.add_argument("pr", nargs="?", help="PR number/url/branch")
    parser.add_argument("--repo", help="repository in OWNER/REPO format")
    methods = parser.add_mutually_exclusive_group()
    for method in ("merge", "squash", "rebase"):
        methods.add_argument(
            f"--{method}",
            dest="merge_method",
            action="store_const",
            const=method,
            help=f"use {method} for a direct merge",
        )
    parser.add_argument("--head", help="expected PR head SHA; defaults to the freshly fetched head")
    parser.add_argument("--subject", help="commit title for a direct merge; defaults to GitHub's title")
    add_body_input_arguments(
        parser,
        required=False,
        body_help="commit message for a direct merge",
        file_help="read commit message from file (use `-` for standard input)",
    )
    parser.add_argument(
        "--merge-action",
        choices=("default", "direct_merge", "merge_queue"),
        default="default",
        help="default honors the target branch's merge queue configuration",
    )
    parser.add_argument(
        "--bypass-rules", action="store_true", help="explicitly request bypass of rules that your account may bypass"
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="seconds to poll for a result (default: 60; 0 returns immediately)"
    )
    parser.set_defaults(handler=cmd_pr_merge)

    status = subparsers.add_parser(
        "merge-status", help="inspect or wait for an existing async merge request without resubmitting"
    )
    status.add_argument("request_id", help="merge request UUID")
    status.add_argument("--pr", help="PR number/url/branch")
    status.add_argument("--repo", help="repository in OWNER/REPO format")
    status.add_argument("--timeout", type=int, default=0, help="seconds to poll while pending (default: 0)")
    status.set_defaults(handler=cmd_pr_merge_status)


def cmd_pr_merge(args: Any) -> int:
    _validate_timeout(args.timeout)
    body = None
    if args.body is not None or args.body_file is not None:
        body = resolve_file_or_inline_text(args, text_attr="body", file_attr="body_file")
    if args.merge_action == "merge_queue" and any(
        value is not None for value in (args.merge_method, args.subject, body)
    ):
        raise RuntimeError("merge method, subject, and body are only supported for direct merges")
    client = GitHubClient()
    meta = resolve_subject(selector=args.pr, repo=args.repo, selector_flag="PR", resolver=client.resolve_pull_request)
    if meta.is_draft or meta.state not in {"OPEN", "MERGED"}:
        raise RuntimeError("cannot merge a draft, closed, or unknown-state pull request")
    if not meta.stack_supported:
        raise RuntimeError("native stack metadata is unavailable; cannot determine the merge scope")
    head = meta.head_ref_oid
    if args.head is not None and args.head != head:
        raise RuntimeError("PR head changed; refresh the PR before merging")
    if meta.state == "OPEN" and not head:
        raise RuntimeError("PR head SHA is unavailable; cannot submit a merge request")
    meta = client.fetch_pull_request_stack(meta)
    if meta.stack:
        if not meta.stack.complete or not any(
            entry.number == meta.ref.number and entry.position == meta.stack.position for entry in meta.stack.entries
        ):
            raise RuntimeError("stack entries are incomplete; refresh the PR before merging")
        scope = [
            entry for entry in meta.stack.entries if entry.position <= meta.stack.position and entry.state != "MERGED"
        ]
        if any(entry.is_draft or entry.state != "OPEN" for entry in scope):
            raise RuntimeError("merge scope includes a draft or closed downstack PR")
        print(
            f"Stack merge scope into `{meta.stack.base_ref_name}`: "
            + " → ".join(f"#{entry.number}" for entry in scope),
            flush=True,
        )
    result = client.request_pull_request_merge(
        meta.ref,
        sha=head,
        merge_method=args.merge_method,
        merge_action=args.merge_action,
        subject=args.subject,
        body=body,
        bypass_rules=args.bypass_rules,
    )
    return _wait_for_merge(client, meta.ref, result, timeout=args.timeout)


def cmd_pr_merge_status(args: Any) -> int:
    _validate_timeout(args.timeout)
    request_id = str(UUID(args.request_id))
    client = GitHubClient()
    meta = resolve_subject(selector=args.pr, repo=args.repo, selector_flag="--pr", resolver=client.resolve_pull_request)
    result = client.fetch_pull_request_merge(meta.ref, request_id)
    return _wait_for_merge(client, meta.ref, result, timeout=args.timeout)


def _validate_timeout(timeout: int) -> None:
    if timeout < 0:
        raise RuntimeError("--timeout must be zero or greater")


def _wait_for_merge(client: GitHubClient, ref: PullRequestRef, result: AsyncMergeResult, *, timeout: int) -> int:
    print("## Merge")
    print(f"repo: {ref.owner}/{ref.name}")
    print(f"pr: #{ref.number}")
    if result.existing_request:
        print("existing_request: true (following the existing request; new merge options were not applied)")
    if result.request_id:
        print(f"request_id: {result.request_id}")
    for key in ("merge_method", "merge_action", "expected_head_sha", "bypass_rules"):
        value = getattr(result, key)
        if value is not None:
            print(f"{key}: {str(value).lower() if isinstance(value, bool) else value}")
    if result.status == "pending":
        assert result.request_id is not None
        request_id = result.request_id
        print("status: pending")
        resume = display_command_with(
            f"pr merge-status {result.request_id} --pr {ref.number} --repo {ref.owner}/{ref.name} --timeout 60"
        )
        print(f"⏎ resume: `{resume}`", flush=True)
        deadline = time.monotonic() + timeout
        while result.status == "pending":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("Merge request is still pending; use the request ID above to check again.")
                return 2
            time.sleep(min(2, remaining))
            result = client.fetch_pull_request_merge(ref, request_id)
    print(f"status: {result.status}")
    if result.message:
        print(f"message: {result.message}")
    if result.sha:
        print(f"merge_commit: {result.sha}")
    if result.status == "enqueued":
        print("Added to the merge queue; the PR has not necessarily merged.")
        print(
            f"⏎ check PR: `{display_command_with(f'pr view {ref.number} --repo {ref.owner}/{ref.name} --show meta,mergeability')}`"
        )
    return 1 if result.status == "failed" else 0
