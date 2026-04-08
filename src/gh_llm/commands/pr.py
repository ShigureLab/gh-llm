from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gh_llm.commands.options import (
    add_body_input_arguments,
    add_timeline_window_arguments,
    maybe_resolve_subject,
    parse_timeline_window,
    raise_unknown_option_value,
    resolve_file_or_inline_text,
    resolve_subject,
)
from gh_llm.github_api import GitHubClient
from gh_llm.invocation import display_command_with
from gh_llm.models import PullRequestDiffPage
from gh_llm.pager import DEFAULT_PAGE_SIZE, TimelinePager, build_context_from_meta
from gh_llm.pr_body import build_pull_request_body_scaffold, parse_required_sections
from gh_llm.render import (
    render_checks_section,
    render_comment_node_detail,
    render_description,
    render_event_detail,
    render_event_detail_blocks,
    render_expand_hints,
    render_frontmatter,
    render_header,
    render_hidden_gap,
    render_mergeability_section,
    render_page,
    render_pr_actions,
)

if TYPE_CHECKING:
    from gh_llm.models import (
        PullRequestDiffFile,
        PullRequestMeta,
        ReviewThreadSummary,
        TimelineContext,
        TimelinePage,
    )

DEFAULT_DIFF_HUNK_LINES = 12
DEFAULT_REVIEW_START_FILE_PAGE_SIZE = 5
DEFAULT_NEARBY_THREAD_AUTO_CONTEXT_LINES = 3


@dataclass(frozen=True)
class _ExpandOptions:
    resolved: bool = False
    minimized: bool = False
    details: bool = False


@dataclass(frozen=True)
class _ShowOptions:
    meta: bool = True
    description: bool = True
    timeline: bool = True
    checks: bool = True
    actions: bool = True
    mergeability: bool = True


def register_pr_parser(subparsers: Any) -> None:
    pr_parser = subparsers.add_parser("pr", help="pull request reading, review, and actions")
    pr_subparsers = pr_parser.add_subparsers(dest="pr_command")

    view_parser = pr_subparsers.add_parser(
        "view",
        help="show PR overview with timeline, checks, and actions",
    )
    view_parser.add_argument("pr", nargs="?", help="PR number/url/branch")
    view_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    view_parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="timeline entries per page")
    view_parser.add_argument(
        "--show",
        action="append",
        default=[],
        help="show regions: meta, description, timeline, checks, actions, mergeability, all (comma-separated or repeatable)",
    )
    view_parser.add_argument(
        "--expand",
        action="append",
        default=[],
        help="auto-expand folded content: resolved, minimized, details, all (comma-separated or repeatable)",
    )
    view_parser.add_argument(
        "--diff-hunk-lines",
        type=int,
        default=DEFAULT_DIFF_HUNK_LINES,
        help="max lines for each review diff hunk (<=0 means full)",
    )
    add_timeline_window_arguments(view_parser)
    view_parser.set_defaults(handler=cmd_pr_view)

    timeline_expand_parser = pr_subparsers.add_parser("timeline-expand", help="expand a specific timeline page")
    timeline_expand_parser.add_argument("page", type=int, help="1-based page number")
    timeline_expand_parser.add_argument("--pr", help="PR number/url/branch")
    timeline_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    timeline_expand_parser.add_argument("--page-size", type=int, help="timeline entries per page")
    timeline_expand_parser.add_argument(
        "--expand",
        action="append",
        default=[],
        help="auto-expand folded content: resolved, minimized, details, all (comma-separated or repeatable)",
    )
    timeline_expand_parser.add_argument(
        "--diff-hunk-lines",
        type=int,
        default=DEFAULT_DIFF_HUNK_LINES,
        help="max lines for each review diff hunk (<=0 means full)",
    )
    add_timeline_window_arguments(timeline_expand_parser)
    timeline_expand_parser.set_defaults(handler=cmd_pr_timeline_expand)

    details_expand_parser = pr_subparsers.add_parser(
        "details-expand",
        help="show collapsed <details>/<summary> blocks for one timeline event",
    )
    details_expand_parser.add_argument("index", type=int, help="1-based event index from timeline view")
    details_expand_parser.add_argument("--pr", help="PR number/url/branch")
    details_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    details_expand_parser.add_argument("--page-size", type=int, help="timeline entries per page")
    add_timeline_window_arguments(details_expand_parser)
    details_expand_parser.set_defaults(handler=cmd_pr_details_expand)

    review_expand_parser = pr_subparsers.add_parser(
        "review-expand",
        help="expand review conversations for one or more review IDs",
    )
    review_expand_parser.add_argument(
        "review_ids",
        nargs="+",
        help="review ids, supports values like `PRR_xxx`, `PRR_xxx,PRR_yyy`",
    )
    review_expand_parser.add_argument("--pr", help="PR number/url/branch")
    review_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    review_expand_parser.add_argument("--page-size", type=int, help="timeline entries per page")
    review_expand_parser.add_argument(
        "--threads",
        help="expand specific conversation range like 6-16 (1-based, within current review conversation order)",
    )
    review_expand_parser.add_argument(
        "--diff-hunk-lines",
        type=int,
        default=DEFAULT_DIFF_HUNK_LINES,
        help="max lines for each review diff hunk (<=0 means full)",
    )
    review_expand_parser.set_defaults(handler=cmd_pr_review_expand)

    thread_expand_parser = pr_subparsers.add_parser("thread-expand", help="expand one review thread by thread ID")
    thread_expand_parser.add_argument("thread_id", help="review thread id, e.g. PRRT_xxx")
    thread_expand_parser.add_argument("--pr", help="PR number/url/branch")
    thread_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    thread_expand_parser.add_argument(
        "--diff-hunk-lines",
        type=int,
        default=DEFAULT_DIFF_HUNK_LINES,
        help="max lines for each review diff hunk (<=0 means full)",
    )
    thread_expand_parser.set_defaults(handler=cmd_pr_thread_expand)

    checks_parser = pr_subparsers.add_parser("checks", help="show CI check status for the PR")
    checks_parser.add_argument("--pr", help="PR number/url/branch")
    checks_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    checks_parser.add_argument("--all", action="store_true", help="show all checks including passed")
    checks_parser.set_defaults(handler=cmd_pr_checks)

    body_template_parser = pr_subparsers.add_parser(
        "body-template",
        help="load a repo PR template, fill missing required sections, and write an editable body scaffold",
    )
    body_template_parser.add_argument("--repo", required=True, help="repository in OWNER/REPO format")
    body_template_parser.add_argument("--title", help="optional PR title used in the suggested `gh pr create` command")
    body_template_parser.add_argument(
        "--requirements",
        action="append",
        default=[],
        help="required body sections, comma-separated or repeatable",
    )
    body_template_parser.add_argument(
        "--output",
        help="write the scaffold to this file (defaults to a temporary .md file)",
    )
    body_template_parser.set_defaults(handler=cmd_pr_body_template)

    conflicts_parser = pr_subparsers.add_parser(
        "conflict-files",
        help="detect and show conflicted files for a PR (on demand; may take longer on large repos)",
    )
    conflicts_parser.add_argument("--pr", help="PR number/url/branch")
    conflicts_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    conflicts_parser.set_defaults(handler=cmd_pr_conflict_files)

    thread_reply_parser = pr_subparsers.add_parser("thread-reply", help="reply to a pull request review thread")
    thread_reply_parser.add_argument("thread_id", help="review thread id, e.g. PRRT_xxx")
    add_body_input_arguments(
        thread_reply_parser,
        required=True,
        body_help="reply body",
        file_help="read reply body from file (use `-` to read from standard input)",
    )
    thread_reply_parser.add_argument("--pr", help="PR number/url/branch")
    thread_reply_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    thread_reply_parser.set_defaults(handler=cmd_pr_thread_reply)

    thread_resolve_parser = pr_subparsers.add_parser(
        "thread-resolve", help="mark a pull request review thread as resolved"
    )
    thread_resolve_parser.add_argument("thread_id", help="review thread id, e.g. PRRT_xxx")
    thread_resolve_parser.add_argument("--pr", help="PR number/url/branch")
    thread_resolve_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    thread_resolve_parser.set_defaults(handler=cmd_pr_thread_resolve)

    thread_unresolve_parser = pr_subparsers.add_parser(
        "thread-unresolve", help="mark a pull request review thread as unresolved"
    )
    thread_unresolve_parser.add_argument("thread_id", help="review thread id, e.g. PRRT_xxx")
    thread_unresolve_parser.add_argument("--pr", help="PR number/url/branch")
    thread_unresolve_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    thread_unresolve_parser.set_defaults(handler=cmd_pr_thread_unresolve)

    comment_edit_parser = pr_subparsers.add_parser("comment-edit", help="edit a comment by node id")
    comment_edit_parser.add_argument("comment_id", help="comment id, e.g. IC_xxx or PRRC_xxx")
    add_body_input_arguments(
        comment_edit_parser,
        required=True,
        body_help="new comment body",
        file_help="read new comment body from file (use `-` to read from standard input)",
    )
    comment_edit_parser.add_argument("--pr", help="PR number/url/branch")
    comment_edit_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    comment_edit_parser.set_defaults(handler=cmd_pr_comment_edit)

    comment_expand_parser = pr_subparsers.add_parser("comment-expand", help="expand one comment by node id")
    comment_expand_parser.add_argument("comment_id", help="comment id, e.g. IC_xxx or PRRC_xxx")
    comment_expand_parser.add_argument("--pr", help="PR number/url/branch")
    comment_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    comment_expand_parser.set_defaults(handler=cmd_pr_comment_expand)

    review_start_parser = pr_subparsers.add_parser(
        "review-start",
        help="start a code review with diff hunks and inline comment templates",
    )
    review_start_parser.add_argument("--pr", help="PR number/url/branch")
    review_start_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    review_start_parser.add_argument("--page", type=int, help="1-based changed-file page number")
    review_start_parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_REVIEW_START_FILE_PAGE_SIZE,
        help="changed files per page",
    )
    review_start_parser.add_argument(
        "--files",
        help="1-based changed-file selection, e.g. 6-12 or 3,5-7; bypasses page pagination",
    )
    review_start_parser.add_argument(
        "--path",
        help="focus one changed file by exact path or unique suffix match; bypasses file pagination",
    )
    review_start_parser.add_argument(
        "--hunks",
        help="1-based hunk selection within --path, e.g. 2,4-6",
    )
    review_start_parser.add_argument(
        "--head",
        help="pin review-start to a PR head sha; generated commands will reuse this snapshot",
    )
    review_start_parser.add_argument(
        "--context-lines",
        type=int,
        default=0,
        help="extra unchanged lines before/after each hunk when available",
    )
    review_start_parser.add_argument("--max-hunks", type=int, default=40, help="maximum hunks to render")
    review_start_parser.set_defaults(handler=cmd_pr_review_start)

    review_comment_parser = pr_subparsers.add_parser(
        "review-comment", help="add an inline review comment on a diff line or range"
    )
    review_comment_parser.add_argument("--path", required=True, help="file path in pull request")
    review_comment_parser.add_argument("--line", required=True, type=int, help="ending line number on selected side")
    review_comment_parser.add_argument(
        "--start-line", type=int, help="starting line number for a continuous multi-line range"
    )
    review_comment_parser.add_argument("--side", choices=["RIGHT", "LEFT"], default="RIGHT", help="diff side")
    review_comment_parser.add_argument(
        "--start-side",
        choices=["RIGHT", "LEFT"],
        help="starting diff side for a multi-line range (defaults to --side)",
    )
    review_comment_parser.add_argument("--head", help="expected PR head sha for stale-snapshot protection")
    add_body_input_arguments(
        review_comment_parser,
        required=True,
        body_help="review comment body",
        file_help="read review comment body from file (use `-` to read from standard input)",
    )
    review_comment_parser.add_argument("--pr", help="PR number/url/branch")
    review_comment_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    review_comment_parser.set_defaults(handler=cmd_pr_review_comment)

    review_suggest_parser = pr_subparsers.add_parser(
        "review-suggest", help="add an inline code suggestion on a diff line"
    )
    review_suggest_parser.add_argument("--path", required=True, help="file path in pull request")
    review_suggest_parser.add_argument("--line", required=True, type=int, help="line number on selected side")
    review_suggest_parser.add_argument("--side", choices=["RIGHT", "LEFT"], default="RIGHT", help="diff side")
    add_body_input_arguments(
        review_suggest_parser,
        required=False,
        body_help="review comment body before suggestion block",
        file_help="read review comment body from file (use `-` to read from standard input)",
        default="Suggested change",
    )
    review_suggest_suggestion_group = review_suggest_parser.add_mutually_exclusive_group(required=True)
    review_suggest_suggestion_group.add_argument(
        "--suggestion",
        help="replacement content inserted inside ```suggestion block",
    )
    review_suggest_suggestion_group.add_argument(
        "--suggestion-file",
        help="read replacement content from file (use `-` to read from standard input)",
    )
    review_suggest_parser.add_argument("--head", help="expected PR head sha for stale-snapshot protection")
    review_suggest_parser.add_argument("--pr", help="PR number/url/branch")
    review_suggest_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    review_suggest_parser.set_defaults(handler=cmd_pr_review_suggest)

    review_submit_parser = pr_subparsers.add_parser(
        "review-submit", help="submit a PR review (approve, request changes, or comment)"
    )
    review_submit_parser.add_argument(
        "--event",
        choices=["COMMENT", "APPROVE", "REQUEST_CHANGES"],
        default="COMMENT",
        help="review event type",
    )
    add_body_input_arguments(
        review_submit_parser,
        required=False,
        body_help="review summary body",
        file_help="read review summary body from file (use `-` to read from standard input)",
        default="",
    )
    review_submit_parser.add_argument("--pr", help="PR number/url/branch")
    review_submit_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    review_submit_parser.set_defaults(handler=cmd_pr_review_submit)


def _resolve_body_argument(args: Any, *, default: str = "") -> str:
    return resolve_file_or_inline_text(args, text_attr="body", file_attr="body_file", default=default)


def _resolve_suggestion_argument(args: Any) -> str:
    return resolve_file_or_inline_text(args, text_attr="suggestion", file_attr="suggestion_file")


def _validate_review_suggest_stdin_sources(args: Any) -> None:
    if getattr(args, "body_file", None) == "-" and getattr(args, "suggestion_file", None) == "-":
        raise RuntimeError(
            "`--body-file -` cannot be combined with `--suggestion-file -`; standard input can only be consumed once"
        )


def _resolve_review_submit_body(args: Any) -> str:
    return _resolve_body_argument(args)


def cmd_pr_view(args: Any) -> int:
    page_size = int(args.page_size)
    diff_hunk_lines = _resolve_diff_hunk_lines(args=args, default=DEFAULT_DIFF_HUNK_LINES)
    expand = _parse_expand_options(raw_values=list(getattr(args, "expand", [])))
    show = _parse_show_options(raw_values=list(getattr(args, "show", [])))
    timeline_window = _resolve_timeline_window(args)
    client = GitHubClient()
    pager = TimelinePager(client)

    meta = _resolve_pr_meta(client=client, args=args)
    context = build_context_from_meta(meta=meta, page_size=page_size)
    first_page: TimelinePage | None = None
    last_page: TimelinePage | None = None
    shown_pages: set[int] = set()

    if show.timeline:
        context, first_page, last_page = pager.build_initial(
            meta,
            page_size=page_size,
            timeline_window=timeline_window,
            show_resolved_details=expand.resolved,
            show_outdated_details=True,
            show_minimized_details=expand.minimized,
            show_details_blocks=expand.details,
            diff_hunk_lines=diff_hunk_lines,
        )
        shown_pages.add(1)

    wrote_output = False

    def print_block(lines: list[str]) -> None:
        nonlocal wrote_output
        if not lines:
            return
        if wrote_output:
            print()
        for line in lines:
            print(line)
        wrote_output = True

    if show.meta:
        print_block(render_frontmatter(context))
    if show.description:
        print_block(render_description(context))
    if show.timeline:
        assert first_page is not None
        print_block(["## Timeline"])
        print_block(render_page(1, context, first_page))

    if show.timeline and last_page is not None:
        trailing_pages: list[tuple[int, TimelinePage]] = []
        include_previous = context.total_pages > 2 and context.total_count % context.page_size != 0
        if include_previous:
            previous_page_number = context.total_pages - 1
            previous_page = pager.fetch_page(
                meta=meta,
                context=context,
                page=previous_page_number,
                show_resolved_details=expand.resolved,
                show_outdated_details=True,
                show_minimized_details=expand.minimized,
                show_details_blocks=expand.details,
                diff_hunk_lines=diff_hunk_lines,
            )
            trailing_pages.append((previous_page_number, previous_page))
            shown_pages.add(previous_page_number)

        trailing_pages.append((context.total_pages, last_page))
        shown_pages.add(context.total_pages)

        first_trailing_page = min(page_number for page_number, _ in trailing_pages)
        hidden_start = 2
        hidden_end = first_trailing_page - 1
        hidden_pages = list(range(hidden_start, hidden_end + 1)) if hidden_start <= hidden_end else []
        print_block(render_hidden_gap(context, hidden_pages))
        if hidden_pages:
            print()
        for index, (page_number, page_data) in enumerate(trailing_pages):
            if index > 0:
                print()
            for line in render_page(page_number, context, page_data):
                print(line)

    if show.timeline:
        print_block(render_expand_hints(context, shown_pages))
    checks: list[Any] = []
    if show.checks or show.mergeability:
        checks = client.fetch_checks(meta.ref) if meta.state == "OPEN" else []
    if show.checks:
        print_block(
            render_checks_section(
                context=context,
                checks=checks,
                show_all=False,
                is_open=(meta.state == "OPEN"),
            )
        )
    if show.actions:
        print_block(
            render_pr_actions(
                context,
                include_diff=True,
                include_manage=show.actions,
            )
        )
    if show.mergeability:
        print_block(render_mergeability_section(context=context, checks=checks))

    return 0


def cmd_pr_timeline_expand(args: Any) -> int:
    client = GitHubClient()
    pager = TimelinePager(client)
    diff_hunk_lines = _resolve_diff_hunk_lines(args=args, default=DEFAULT_DIFF_HUNK_LINES)
    expand = _parse_expand_options(raw_values=list(getattr(args, "expand", [])))
    context, meta = _resolve_context_and_meta(
        client=client,
        pager=pager,
        args=args,
        show_resolved_details=expand.resolved,
        show_outdated_details=True,
        show_minimized_details=expand.minimized,
        show_details_blocks=expand.details,
        diff_hunk_lines=diff_hunk_lines,
    )

    page = pager.fetch_page(
        meta=meta,
        context=context,
        page=int(args.page),
        show_resolved_details=expand.resolved,
        show_outdated_details=True,
        show_minimized_details=expand.minimized,
        show_details_blocks=expand.details,
        diff_hunk_lines=diff_hunk_lines,
    )

    for line in render_header(context):
        print(line)
    print("## Timeline")
    for line in render_page(int(args.page), context, page):
        print(line)

    return 0


def cmd_pr_details_expand(args: Any) -> int:
    client = GitHubClient()
    pager = TimelinePager(client)
    context, meta = _resolve_context_and_meta(
        client=client,
        pager=pager,
        args=args,
        show_resolved_details=True,
        show_outdated_details=True,
        show_minimized_details=True,
        show_details_blocks=True,
        diff_hunk_lines=None,
    )

    index = int(args.index)
    page_number = _resolve_timeline_page_for_index(context=context, index=index)
    if page_number is None:
        raise RuntimeError(f"invalid event index {index}, expected in 1..{context.total_count}")

    page = pager.fetch_page(
        meta=meta,
        context=context,
        page=page_number,
        show_resolved_details=True,
        show_outdated_details=True,
        show_minimized_details=True,
        show_details_blocks=True,
        diff_hunk_lines=None,
    )
    try:
        offset = page.absolute_indexes.index(index)
    except ValueError:
        raise RuntimeError("event index is outside loaded page range") from None

    for line in render_event_detail_blocks(index=index, event=page.items[offset]):
        print(line)
    return 0


def cmd_pr_review_expand(args: Any) -> int:
    client = GitHubClient()
    pager = TimelinePager(client)
    context, meta = _resolve_context_and_meta(client=client, pager=pager, args=args)
    review_ids = parse_review_ids(args.review_ids)
    thread_range = _parse_thread_range(getattr(args, "threads", None))
    diff_hunk_lines = _resolve_diff_hunk_lines(args=args, default=DEFAULT_DIFF_HUNK_LINES)
    if diff_hunk_lines is not None:
        print("Δ Diff hunk window is limited; rerun with `--diff-hunk-lines 0` for full review diff context.")
        print()

    if thread_range is not None:
        start, end = thread_range
        for review_id in review_ids:
            print(f"## Review {review_id}")
            lines = client.expand_review(
                ref=meta.ref,
                review_id=review_id,
                thread_start=start,
                thread_end=end,
                show_resolved_details=False,
                show_details_blocks=False,
                diff_hunk_lines=diff_hunk_lines,
            )
            for line in lines:
                print(line)
            print()
        return 0

    matched: dict[str, tuple[int, TimelinePage]] = {}
    for page_number in range(1, context.total_pages + 1):
        page = pager.fetch_page(
            meta=meta,
            context=context,
            page=page_number,
            show_resolved_details=True,
            show_outdated_details=True,
            show_minimized_details=True,
            show_details_blocks=False,
            review_threads_window=None,
            diff_hunk_lines=diff_hunk_lines,
        )
        for offset, event in enumerate(page.items):
            if not event.kind.startswith("review/"):
                continue
            if event.source_id in review_ids:
                event_index = page.absolute_indexes[offset]
                matched[event.source_id] = (event_index, page)
        if len(matched) == len(review_ids):
            break

    for review_id in review_ids:
        item = matched.get(review_id)
        if item is None:
            print(f"## Review {review_id}")
            print("(not found on this PR timeline)")
            print()
            continue

        event_index, page = item
        try:
            offset = page.absolute_indexes.index(event_index)
        except ValueError:
            print(f"## Review {review_id}")
            print("(not found on this PR timeline)")
            print()
            continue
        event = page.items[offset]
        for line in render_event_detail(index=event_index, event=event):
            print(line)
        print()

    return 0


def cmd_pr_thread_expand(args: Any) -> int:
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    diff_hunk_lines = _resolve_diff_hunk_lines(args=args, default=DEFAULT_DIFF_HUNK_LINES)
    review_id, lines = client.expand_review_thread(
        ref=meta.ref,
        thread_id=str(args.thread_id),
        show_details_blocks=False,
        diff_hunk_lines=diff_hunk_lines,
    )
    print(f"## Review Thread {args.thread_id}")
    print(f"review_id: {review_id}")
    for line in lines:
        print(line)
    return 0


def cmd_pr_checks(args: Any) -> int:
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    context = build_context_from_meta(meta=meta, page_size=DEFAULT_PAGE_SIZE)
    checks = client.fetch_checks(meta.ref) if meta.state == "OPEN" else []
    for line in render_checks_section(
        context=context,
        checks=checks,
        show_all=bool(args.all),
        is_open=(meta.state == "OPEN"),
    ):
        print(line)
    return 0


def cmd_pr_body_template(args: Any) -> int:
    client = GitHubClient()
    repo = str(args.repo)
    required_sections = parse_required_sections(list(getattr(args, "requirements", [])))
    template_path, template_text = client.fetch_pull_request_template(repo)
    scaffold = build_pull_request_body_scaffold(template_text, required_sections=required_sections)
    output_path = _resolve_pr_body_output_path(getattr(args, "output", None))
    output_path.write_text(scaffold.body, encoding="utf-8")

    title = str(args.title).strip() if getattr(args, "title", None) else ""
    quoted_repo = shlex.quote(repo)
    quoted_output_path = shlex.quote(str(output_path))
    quoted_title = shlex.quote(title) if title else "'<pr_title>'"

    print("## PR Body Scaffold")
    print(f"repo: {repo}")
    print(f"template_found: {'true' if template_path else 'false'}")
    print(f"template_path: {template_path or '(none)'}")
    print(f"output_file: {output_path}")
    print(f"required_sections: {json.dumps(required_sections, ensure_ascii=False)}")
    print(f"added_sections: {json.dumps(list(scaffold.added_sections), ensure_ascii=False)}")
    print()
    print("## Body")
    print("<pr_body>")
    print(scaffold.body.rstrip())
    print("</pr_body>")
    print()
    print("## Actions")
    if not title:
        print("⌨ pr_title: '<pr_title>'")
    print(f"⏎ Edit scaffold file: `{quoted_output_path}`")
    print(
        f"⏎ Create PR via gh: `gh pr create --repo {quoted_repo} --title {quoted_title} --body-file {quoted_output_path}`"
    )
    return 0


def _resolve_pr_body_output_path(raw_output: object) -> Path:
    if raw_output is None:
        fd, temp_path = tempfile.mkstemp(prefix="gh-llm-pr-body-", suffix=".md")
        os.close(fd)
        return Path(temp_path)

    path = Path(str(raw_output)).expanduser()
    if path.exists() and path.is_dir():
        raise RuntimeError(f"output path is a directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def cmd_pr_conflict_files(args: Any) -> int:
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    files = client.fetch_conflict_files(meta)
    repo = f"{meta.ref.owner}/{meta.ref.name}"
    print("## Conflict Files")
    print(f"PR: {meta.ref.number} ({repo})")
    if not files:
        print("(no conflicted files detected, or unable to detect)")
        return 0
    for path in files:
        print(f"- `{path}`")
    return 0


def cmd_pr_thread_reply(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_pr(client=client, args=args)

    body = _resolve_body_argument(args)
    comment_id = client.reply_review_thread(thread_id=str(args.thread_id), body=body)
    print(f"thread: {args.thread_id}")
    if comment_id:
        print(f"reply_comment_id: {comment_id}")
    print("status: replied")
    return 0


def cmd_pr_thread_resolve(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_pr(client=client, args=args)

    resolved = client.resolve_review_thread(thread_id=str(args.thread_id))
    print(f"thread: {args.thread_id}")
    print(f"status: {'resolved' if resolved else 'unchanged'}")
    return 0


def cmd_pr_thread_unresolve(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_pr(client=client, args=args)

    resolved = client.unresolve_review_thread(thread_id=str(args.thread_id))
    print(f"thread: {args.thread_id}")
    print(f"status: {'still_resolved' if resolved else 'unresolved'}")
    return 0


def cmd_pr_comment_edit(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_pr(client=client, args=args)
    updated_comment_id = client.edit_comment(comment_id=str(args.comment_id), body=_resolve_body_argument(args))
    print(f"comment: {updated_comment_id}")
    print("status: edited")
    return 0


def cmd_pr_comment_expand(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_pr(client=client, args=args)
    node = client.fetch_comment_node(str(args.comment_id))
    for line in render_comment_node_detail(str(args.comment_id), node):
        print(line)
    return 0


def cmd_pr_review_start(args: Any) -> int:
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    pinned_head = _validate_pr_head_snapshot(meta=meta, requested_head=_resolve_requested_head(args))
    file_selection_raw = _resolve_review_start_files(args)
    path_filter = _resolve_review_start_path(args)
    hunk_selection_raw = _resolve_review_start_hunks(args)
    if file_selection_raw is not None and path_filter is not None:
        raise RuntimeError("`--files` cannot be combined with `--path`")
    if file_selection_raw is not None and getattr(args, "page", None) is not None:
        raise RuntimeError("`--files` cannot be combined with `--page`")
    if hunk_selection_raw is not None and path_filter is None:
        raise RuntimeError("`--hunks` requires `--path`")
    max_hunks = max(1, int(args.max_hunks))
    context_lines = _resolve_review_start_context_lines(args)
    page_size = _resolve_review_start_page_size(args)
    repo = f"{meta.ref.owner}/{meta.ref.name}"

    focused_file: PullRequestDiffFile | None = None
    focused_hunks: list[_DiffHunk] | None = None
    diff_page = None
    file_entries: list[tuple[int, PullRequestDiffFile, list[_DiffHunk]]] = []
    total_hunks_on_page = 0
    file_start = 0
    file_end = 0
    selected_file_indexes: list[int] | None = None

    if path_filter is not None:
        focused_file, focused_page, focused_index = _resolve_review_start_file(
            client=client,
            meta=meta,
            path_filter=path_filter,
        )
        focused_hunks = _extract_diff_hunks_from_review_file(focused_file)
        diff_page = focused_page
        file_entries = [(focused_index, focused_file, focused_hunks)]
        total_hunks_on_page = len(focused_hunks)
        file_start = focused_index
        file_end = focused_index
    elif file_selection_raw is not None:
        selected_file_indexes = _resolve_review_start_file_indexes(
            raw=file_selection_raw,
            total_files=(meta.changed_files or 0),
        )
        selected_files = _fetch_review_start_files_by_index(
            client=client,
            meta=meta,
            file_indexes=selected_file_indexes,
        )
        diff_page = PullRequestDiffPage(
            page=1,
            page_size=len(selected_files),
            total_files=(meta.changed_files or len(selected_files)),
            total_pages=1,
            files=tuple(file for _, file in selected_files),
        )
        file_entries = [
            (file_index, file, _extract_diff_hunks_from_review_file(file)) for file_index, file in selected_files
        ]
        total_hunks_on_page = sum(len(hunks) for _, _, hunks in file_entries)
        file_start = selected_file_indexes[0]
        file_end = selected_file_indexes[-1]
    else:
        page = _resolve_review_start_page(args)
        diff_page = client.fetch_pr_files_page(meta, page=page, page_size=page_size)
        file_entries = [
            (file_start_index, file, _extract_diff_hunks_from_review_file(file))
            for file_start_index, file in enumerate(
                diff_page.files,
                start=((diff_page.page - 1) * diff_page.page_size) + 1,
            )
        ]
        total_hunks_on_page = sum(len(hunks) for _, _, hunks in file_entries)
        file_start = (
            ((diff_page.page - 1) * diff_page.page_size) + 1 if diff_page.total_files > 0 and diff_page.files else 0
        )
        file_end = file_start + len(diff_page.files) - 1 if diff_page.files else 0

    print("## Review Start")
    print(f"PR: {meta.ref.number} ({repo})")
    if pinned_head is not None:
        print(f"Head snapshot: {pinned_head}")
    print(f"Files changed: {diff_page.total_files}")
    if path_filter is not None and focused_file is not None:
        print(f"Focused file: {focused_file.path} ({file_start}/{diff_page.total_files})")
    elif selected_file_indexes is not None:
        print(f"Selected files: {_format_line_spans(selected_file_indexes)} of {diff_page.total_files}")
    else:
        if diff_page.files:
            print(
                f"File page: {diff_page.page}/{diff_page.total_pages} ({file_start}-{file_end} of {diff_page.total_files})"
            )
        else:
            print(f"File page: {diff_page.page}/{diff_page.total_pages}")
    print(f"Hunks on this page: {total_hunks_on_page}")
    if context_lines > 0:
        print(f"Extra context lines: {context_lines}")
    print(f"Δ full diff: `gh pr diff {meta.ref.number} --repo {repo}`")
    head_flag = f" --head {pinned_head}" if pinned_head is not None else ""
    comment_template_cmd = display_command_with(
        f"pr review-comment --path '<path>' --line <line> --side RIGHT --body '<review_comment>'{head_flag} --pr {meta.ref.number} --repo {repo}"
    )
    suggestion_template_cmd = display_command_with(
        f"pr review-suggest --path '<path>' --line <line> --side RIGHT --body '<reason>' --suggestion '<replacement>'{head_flag} --pr {meta.ref.number} --repo {repo}"
    )
    range_template_cmd = display_command_with(
        f"pr review-comment --path '<path>' --start-line <start_line> --line <line> --side RIGHT --body '<review_comment>'{head_flag} --pr {meta.ref.number} --repo {repo}"
    )
    print(f"Comment template: `{comment_template_cmd}`")
    print(f"Suggestion template: `{suggestion_template_cmd}`")
    print(f"Multi-line template: `{range_template_cmd}`")
    print()

    if not diff_page.files:
        print("(no changed files found)")
        return 0

    thread_summaries = client.fetch_review_thread_summaries(meta.ref)
    if thread_summaries:
        thread_template_cmd = display_command_with(f"pr thread-expand <thread_id> --pr {meta.ref.number} --repo {repo}")
        print(f"Thread detail: `{thread_template_cmd}`")
        print()

    thread_summaries_by_path = _group_review_thread_summaries_by_path(thread_summaries)
    remaining_hunks = max_hunks
    rendered_hunks = 0
    for file_index, file, hunks in file_entries:
        if remaining_hunks <= 0:
            break
        print(f"### File {file_index}/{diff_page.total_files}: {file.path}")
        print(f"Status: {_format_review_file_status(file)}")
        if file.previous_path and file.previous_path != file.path:
            print(f"Previous path: {file.previous_path}")
        file_thread_summaries = _filter_review_start_thread_summaries(
            _resolve_review_start_thread_summaries_for_file(
                file=file,
                summaries_by_path=thread_summaries_by_path,
            )
        )
        if file_thread_summaries:
            print(_format_review_thread_file_summary(file_thread_summaries))
            file_scoped_summaries = [
                summary for summary in file_thread_summaries if _is_file_scoped_thread_summary(summary)
            ]
            if file_scoped_summaries:
                print("File-scoped review threads:")
                for line in _render_review_thread_summary_items(file_scoped_summaries):
                    print(line)
        if not file.patch:
            print("(no patch preview available from GitHub API for this file)")
            print()
            continue
        print(f"Hunks: {len(hunks)}")
        if not hunks:
            print("(no diff hunks parsed from this file patch)")
            print()
            continue
        hunk_indexes = _resolve_review_start_hunk_indexes(
            raw=hunk_selection_raw,
            total_hunks=len(hunks),
        )
        visible_hunks = (
            [hunks[index - 1] for index in hunk_indexes] if hunk_indexes is not None else hunks[:remaining_hunks]
        )
        effective_context_lines_by_hunk = [
            max(
                context_lines,
                _resolve_nearby_thread_context_lines(
                    hunk=hunk,
                    summaries=file_thread_summaries,
                ),
            )
            for hunk in visible_hunks
        ]
        file_snapshot_lines = _load_review_start_file_snapshot_lines(
            client=client,
            meta=meta,
            file=file,
            context_lines=max(effective_context_lines_by_hunk, default=context_lines),
        )
        extra_contexts = [
            _build_review_start_hunk_context_lines(
                hunk=hunk,
                file=file,
                snapshot_lines=file_snapshot_lines,
                context_lines=effective_context_lines_by_hunk[index],
            )
            for index, hunk in enumerate(visible_hunks)
        ]
        inline_thread_blocks_by_hunk = _build_inline_review_thread_blocks_for_file(
            hunks=visible_hunks,
            summaries=file_thread_summaries,
            extra_contexts=extra_contexts,
        )
        for hunk_index, hunk in enumerate(visible_hunks, start=1):
            display_hunk_index = hunk_indexes[hunk_index - 1] if hunk_indexes is not None else hunk_index
            print(f"#### Hunk {display_hunk_index}")
            print(f"Header: {hunk.header}")
            left_span_preview = _format_line_spans(sorted(hunk.left_commentable_lines))
            right_span_preview = _format_line_spans(sorted(hunk.right_commentable_lines))
            print(f"LEFT commentable span(s): {left_span_preview}")
            print(f"RIGHT commentable span(s): {right_span_preview}")
            extra_context = extra_contexts[hunk_index - 1]
            inline_thread_blocks = inline_thread_blocks_by_hunk[hunk_index - 1]
            print("Use the L#### / R#### labels from the numbered diff below as --line values.")
            print("For a continuous multi-line range on the same side, add --start-line <start_line>.")
            print("```text")
            for line in _render_numbered_hunk_lines(
                hunk,
                extra_context_lines=extra_context,
                inline_thread_blocks=inline_thread_blocks,
            ):
                print(line)
            print("```")
            print()
        rendered_hunks += len(visible_hunks)
        if hunk_indexes is None:
            remaining_hunks -= len(visible_hunks)

    hidden_hunks = total_hunks_on_page - rendered_hunks
    if hidden_hunks > 0 and hunk_selection_raw is None:
        rerun_cmd = display_command_with(
            _build_review_start_command(
                meta=meta,
                page=(None if path_filter is not None else diff_page.page),
                page_size=page_size,
                max_hunks=total_hunks_on_page,
                head=pinned_head,
                context_lines=context_lines,
                files=file_selection_raw,
                path=path_filter,
                hunks=hunk_selection_raw,
            )
        )
        print(f"... {hidden_hunks} hunks hidden on this page by --max-hunks")
        print(f"⏎ rerun this page with a larger hunk cap: `{rerun_cmd}`")
        print()

    previous_page_cmd = None
    next_page_cmd = None
    previous_files_cmd = None
    next_files_cmd = None
    if selected_file_indexes is not None:
        previous_files_cmd, next_files_cmd = _review_start_file_selection_commands(
            meta=meta,
            file_indexes=selected_file_indexes,
            max_hunks=max_hunks,
            head=pinned_head,
            context_lines=context_lines,
        )
    elif path_filter is None:
        previous_page_cmd = _review_start_page_command(
            meta=meta,
            page=(diff_page.page - 1) if diff_page.page > 1 else None,
            page_size=page_size,
            max_hunks=max_hunks,
            head=pinned_head,
            context_lines=context_lines,
        )
        next_page_cmd = _review_start_page_command(
            meta=meta,
            page=(diff_page.page + 1) if diff_page.page < diff_page.total_pages else None,
            page_size=page_size,
            max_hunks=max_hunks,
            head=pinned_head,
            context_lines=context_lines,
        )
    if (
        previous_files_cmd is not None
        or next_files_cmd is not None
        or previous_page_cmd is not None
        or next_page_cmd is not None
    ):
        print("---")
        if previous_files_cmd is not None:
            print(f"⏎ previous file selection: `{previous_files_cmd}`")
        if next_files_cmd is not None:
            print(f"⏎ next file selection: `{next_files_cmd}`")
        if previous_page_cmd is not None:
            print(f"⏎ previous file page: `{previous_page_cmd}`")
        if next_page_cmd is not None:
            print(f"⏎ next file page: `{next_page_cmd}`")
        print("---")
    return 0


def cmd_pr_review_comment(args: Any) -> int:
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    _validate_pr_head_snapshot(meta=meta, requested_head=_resolve_requested_head(args))
    start_line = _resolve_start_line(args)
    start_side = _resolve_start_side(args)
    _validate_review_thread_target(
        client=client,
        args=args,
        path=str(args.path),
        line=int(args.line),
        side=str(args.side),
        start_line=start_line,
        start_side=start_side,
    )
    thread_id, comment_id = client.add_pull_request_review_thread_comment(
        ref=meta.ref,
        path=str(args.path),
        line=int(args.line),
        side=str(args.side),
        start_line=start_line,
        start_side=start_side,
        body=_resolve_body_argument(args),
    )
    print(f"thread: {thread_id}")
    if comment_id:
        print(f"comment: {comment_id}")
    print("status: commented")
    return 0


def cmd_pr_review_suggest(args: Any) -> int:
    _validate_review_suggest_stdin_sources(args)
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    _validate_pr_head_snapshot(meta=meta, requested_head=_resolve_requested_head(args))
    start_line = _resolve_start_line(args)
    start_side = _resolve_start_side(args)
    _validate_review_thread_target(
        client=client,
        args=args,
        path=str(args.path),
        line=int(args.line),
        side=str(args.side),
        start_line=start_line,
        start_side=start_side,
    )
    suggestion = _resolve_suggestion_argument(args).rstrip("\n")
    body = _resolve_body_argument(args, default="Suggested change")
    full_body = f"{body.rstrip()}\n\n```suggestion\n{suggestion}\n```"
    thread_id, comment_id = client.add_pull_request_review_thread_comment(
        ref=meta.ref,
        path=str(args.path),
        line=int(args.line),
        side=str(args.side),
        start_line=start_line,
        start_side=start_side,
        body=full_body,
    )
    print(f"thread: {thread_id}")
    if comment_id:
        print(f"comment: {comment_id}")
    print("status: suggested")
    return 0


def cmd_pr_review_submit(args: Any) -> int:
    client = GitHubClient()
    meta = _resolve_pr_meta(client=client, args=args)
    body = _resolve_review_submit_body(args)
    review_id, review_state = client.submit_pull_request_review(
        ref=meta.ref,
        event=str(args.event),
        body=body if body else None,
    )
    print(f"review: {review_id}")
    if review_state:
        print(f"state: {review_state}")
    print("status: submitted")
    return 0


def _resolve_context_and_meta(
    *,
    client: GitHubClient,
    pager: TimelinePager,
    args: Any,
    show_resolved_details: bool = False,
    show_outdated_details: bool = False,
    show_minimized_details: bool = False,
    show_details_blocks: bool = False,
    diff_hunk_lines: int | None = DEFAULT_DIFF_HUNK_LINES,
) -> tuple[TimelineContext, PullRequestMeta]:
    page_size = getattr(args, "page_size", None)
    effective_page_size = DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
    meta = _resolve_pr_meta(client=client, args=args)
    context, _, _ = pager.build_initial(
        meta=meta,
        page_size=effective_page_size,
        timeline_window=_resolve_timeline_window(args),
        show_resolved_details=show_resolved_details,
        show_outdated_details=show_outdated_details,
        show_minimized_details=show_minimized_details,
        show_details_blocks=show_details_blocks,
        diff_hunk_lines=diff_hunk_lines,
    )
    return context, meta


def _resolve_timeline_window(args: Any):
    return parse_timeline_window(after=getattr(args, "after", None), before=getattr(args, "before", None))


def _resolve_timeline_page_for_index(*, context: TimelineContext, index: int) -> int | None:
    if context.timeline_filtered:
        for page_number, page in context.filtered_pages.items():
            if index in page.absolute_indexes:
                return page_number
        return None
    if index < 1 or index > context.total_count:
        return None
    return ((index - 1) // context.page_size) + 1


def _resolve_diff_hunk_lines(*, args: Any, default: int) -> int | None:
    raw = getattr(args, "diff_hunk_lines", None)
    if raw is None:
        raw = default
    value = int(raw)
    if value <= 0:
        return None
    return value


def _parse_expand_options(*, raw_values: list[str]) -> _ExpandOptions:
    resolved = False
    minimized = False
    details = False

    aliases: dict[str, str] = {
        "resolved": "resolved",
        "resolve": "resolved",
        "resolved_comments": "resolved",
        "resolved-comments": "resolved",
        "minimized": "minimized",
        "details": "details",
        "detail": "details",
        "all": "all",
        "*": "all",
    }
    valid_values = ["resolved", "minimized", "details", "all"]
    alias_values = [alias for alias in aliases if alias not in valid_values and alias != "*"]

    for raw in raw_values:
        for part in raw.split(","):
            token = part.strip().lower()
            if not token:
                continue
            normalized = aliases.get(token)
            if normalized is None:
                raise_unknown_option_value(
                    flag="expand",
                    token=token,
                    valid_values=valid_values,
                    alias_values=alias_values,
                )
            if normalized == "all":
                resolved = True
                minimized = True
                details = True
                continue
            if normalized == "resolved":
                resolved = True
            elif normalized == "minimized":
                minimized = True
            elif normalized == "details":
                details = True

    return _ExpandOptions(resolved=resolved, minimized=minimized, details=details)


def _parse_show_options(*, raw_values: list[str]) -> _ShowOptions:
    if not raw_values:
        return _ShowOptions()

    selected: set[str] = set()
    aliases: dict[str, set[str]] = {
        "meta": {"meta"},
        "description": {"description"},
        "desc": {"description"},
        "timeline": {"timeline"},
        "checks": {"checks"},
        "actions": {"actions"},
        "mergeability": {"mergeability"},
        "merge": {"mergeability"},
        "summary": {"meta", "description"},
        "all": {"meta", "description", "timeline", "checks", "actions", "mergeability"},
        "*": {"meta", "description", "timeline", "checks", "actions", "mergeability"},
    }
    valid_values = ["meta", "description", "timeline", "checks", "actions", "mergeability", "summary", "all"]
    alias_values = [alias for alias in aliases if alias not in valid_values and alias != "*"]

    for raw in raw_values:
        for part in raw.split(","):
            token = part.strip().lower()
            if not token:
                continue
            mapped = aliases.get(token)
            if mapped is None:
                raise_unknown_option_value(
                    flag="show",
                    token=token,
                    valid_values=valid_values,
                    alias_values=alias_values,
                )
            selected.update(mapped)

    return _ShowOptions(
        meta=("meta" in selected),
        description=("description" in selected),
        timeline=("timeline" in selected),
        checks=("checks" in selected),
        actions=("actions" in selected),
        mergeability=("mergeability" in selected),
    )


def _resolve_pr_meta(*, client: GitHubClient, args: Any) -> PullRequestMeta:
    return resolve_subject(
        selector=getattr(args, "pr", None),
        repo=getattr(args, "repo", None),
        selector_flag="--pr",
        resolver=client.resolve_pull_request,
    )


def _resolve_optional_pr(*, client: GitHubClient, args: Any) -> PullRequestMeta | None:
    return maybe_resolve_subject(
        selector=getattr(args, "pr", None),
        repo=getattr(args, "repo", None),
        selector_flag="--pr",
        resolver=client.resolve_pull_request,
    )


class _DiffHunk:
    def __init__(
        self,
        path: str,
        header: str,
        anchor_line: int,
        lines: list[str],
        *,
        left_commentable_lines: set[int],
        right_commentable_lines: set[int],
        match_paths: set[str],
    ) -> None:
        self.path = path
        self.header = header
        self.anchor_line = anchor_line
        self.lines = lines
        self.left_commentable_lines = left_commentable_lines
        self.right_commentable_lines = right_commentable_lines
        self.match_paths = match_paths


_HUNK_HEADER_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")


def _render_numbered_hunk_lines(
    hunk: _DiffHunk,
    *,
    extra_context_lines: tuple[
        list[tuple[int | None, int | None, str]],
        list[tuple[int | None, int | None, str]],
    ]
    | None = None,
    inline_thread_blocks: dict[tuple[str, int], list[str]] | None = None,
) -> list[str]:
    rendered = [hunk.header]
    match = _HUNK_HEADER_RE.match(hunk.header)
    old_line = int(match.group("old")) if match is not None else 1
    new_line = int(match.group("new")) if match is not None else 1

    def format_line(left: int | None, right: int | None, raw: str) -> str:
        left_label = f"L{left:>4}" if left is not None else "L    "
        right_label = f"R{right:>4}" if right is not None else "R    "
        return f"{left_label} {right_label} | {raw}"

    leading_extra_lines: list[tuple[int | None, int | None, str]] = []
    trailing_extra_lines: list[tuple[int | None, int | None, str]] = []
    if extra_context_lines is not None:
        leading_extra_lines, trailing_extra_lines = extra_context_lines

    def append_annotations(left: int | None, right: int | None) -> None:
        if not inline_thread_blocks:
            return
        seen: set[tuple[str, int]] = set()
        for key in ((("LEFT", left) if left is not None else None), (("RIGHT", right) if right is not None else None)):
            if key is None or key in seen:
                continue
            seen.add(key)
            for line in inline_thread_blocks.get(key, []):
                rendered.append(f"            ┆ {line}")

    for left, right, raw in leading_extra_lines:
        rendered.append(format_line(left, right, raw))
        append_annotations(left, right)

    for raw in hunk.lines[1:]:
        marker = raw[:1]
        if marker == "+":
            rendered.append(format_line(None, new_line, raw))
            append_annotations(None, new_line)
            new_line += 1
        elif marker == " ":
            rendered.append(format_line(old_line, new_line, raw))
            append_annotations(old_line, new_line)
            old_line += 1
            new_line += 1
        elif marker == "-":
            rendered.append(format_line(old_line, None, raw))
            append_annotations(old_line, None)
            old_line += 1
        else:
            rendered.append(f"            | {raw}")

    for left, right, raw in trailing_extra_lines:
        rendered.append(format_line(left, right, raw))
        append_annotations(left, right)
    return rendered


def _extract_diff_hunks_from_review_file(file: PullRequestDiffFile) -> list[_DiffHunk]:
    diff = _synthesize_diff_for_review_file(file)
    if diff is None:
        return []
    return _extract_diff_hunks(diff)


def _synthesize_diff_for_review_file(file: PullRequestDiffFile) -> str | None:
    patch = (file.patch or "").strip("\n")
    if not patch:
        return None

    old_path = file.previous_path or file.path
    if file.status == "added":
        old_header = "--- /dev/null"
        new_header = f"+++ b/{file.path}"
    elif file.status == "removed":
        old_header = f"--- a/{old_path}"
        new_header = "+++ /dev/null"
    else:
        old_header = f"--- a/{old_path}"
        new_header = f"+++ b/{file.path}"

    return "\n".join(
        [
            f"diff --git a/{old_path} b/{file.path}",
            old_header,
            new_header,
            patch,
        ]
    )


def _extract_diff_hunks(diff: str) -> list[_DiffHunk]:
    hunks: list[_DiffHunk] = []
    current_old_path = ""
    current_new_path = ""
    current_hunk_header = ""
    current_hunk_lines: list[str] = []
    current_old_line = 0
    current_new_line = 0
    current_anchor = 0
    current_fallback_anchor = 0
    current_left_commentable_lines: set[int] = set()
    current_right_commentable_lines: set[int] = set()

    def resolve_hunk_path() -> tuple[str, set[str]]:
        match_paths = {path for path in (current_old_path, current_new_path) if path}
        if current_new_path:
            return current_new_path, match_paths
        if current_old_path:
            return current_old_path, match_paths
        return "", match_paths

    def flush() -> None:
        nonlocal current_hunk_header, current_hunk_lines, current_anchor, current_fallback_anchor
        nonlocal current_left_commentable_lines, current_right_commentable_lines
        path, match_paths = resolve_hunk_path()
        if path and current_hunk_header and current_hunk_lines:
            anchor_line = (
                current_anchor if current_anchor > 0 else current_fallback_anchor if current_fallback_anchor > 0 else 1
            )
            hunks.append(
                _DiffHunk(
                    path=path,
                    header=current_hunk_header,
                    anchor_line=anchor_line,
                    lines=current_hunk_lines.copy(),
                    left_commentable_lines=current_left_commentable_lines.copy(),
                    right_commentable_lines=current_right_commentable_lines.copy(),
                    match_paths=match_paths,
                )
            )
        current_hunk_header = ""
        current_hunk_lines = []
        current_anchor = 0
        current_fallback_anchor = 0
        current_left_commentable_lines = set()
        current_right_commentable_lines = set()

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            current_old_path = ""
            current_new_path = ""
            continue
        if raw.startswith("--- "):
            flush()
            current_old_path = "" if raw == "--- /dev/null" else raw[len("--- a/") :]
            continue
        if raw.startswith("+++ "):
            flush()
            current_new_path = "" if raw == "+++ /dev/null" else raw[len("+++ b/") :]
            continue

        if raw.startswith("@@ "):
            flush()
            current_hunk_header = raw
            current_hunk_lines = [raw]
            match = _HUNK_HEADER_RE.match(raw)
            if match is None:
                current_old_line = 1
                current_new_line = 1
            else:
                current_old_line = int(match.group("old"))
                current_new_line = int(match.group("new"))
            continue

        if not current_hunk_header:
            continue

        current_hunk_lines.append(raw)
        if raw.startswith("+"):
            if current_anchor <= 0:
                current_anchor = current_new_line
            if current_fallback_anchor <= 0:
                current_fallback_anchor = current_new_line
            current_right_commentable_lines.add(current_new_line)
            current_new_line += 1
        elif raw.startswith(" "):
            if current_fallback_anchor <= 0:
                current_fallback_anchor = current_new_line
            current_left_commentable_lines.add(current_old_line)
            current_right_commentable_lines.add(current_new_line)
            current_old_line += 1
            current_new_line += 1
        elif raw.startswith("-"):
            current_left_commentable_lines.add(current_old_line)
            current_old_line += 1

    flush()
    return hunks


def _resolve_review_start_page(args: Any) -> int:
    raw = getattr(args, "page", None)
    page = 1 if raw is None else int(raw)
    if page < 1:
        raise RuntimeError(f"invalid page {page}, expected >= 1")
    return page


def _resolve_review_start_page_size(args: Any) -> int:
    page_size = int(getattr(args, "page_size", DEFAULT_REVIEW_START_FILE_PAGE_SIZE))
    if page_size < 1 or page_size > 100:
        raise RuntimeError(f"invalid page size {page_size}, expected in 1..100")
    return page_size


def _resolve_review_start_path(args: Any) -> str | None:
    raw = getattr(args, "path", None)
    if raw is None:
        return None
    path = str(raw).strip()
    return path or None


def _resolve_review_start_context_lines(args: Any) -> int:
    value = int(getattr(args, "context_lines", 0))
    if value < 0:
        raise RuntimeError(f"invalid context line count {value}, expected >= 0")
    return value


def _resolve_review_start_files(args: Any) -> str | None:
    raw = getattr(args, "files", None)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _resolve_review_start_hunks(args: Any) -> str | None:
    raw = getattr(args, "hunks", None)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _resolve_requested_head(args: Any) -> str | None:
    raw = getattr(args, "head", None)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value or None


def _validate_pr_head_snapshot(*, meta: PullRequestMeta, requested_head: str | None) -> str | None:
    current_head = (meta.head_ref_oid or "").strip().lower() or None
    if requested_head is None:
        return current_head
    if current_head is None:
        raise RuntimeError("current pull request head sha is unavailable; cannot verify `--head`")
    if current_head == requested_head or current_head.startswith(requested_head):
        return current_head
    repo = f"{meta.ref.owner}/{meta.ref.name}"
    refresh_cmd = display_command_with(f"pr review-start --pr {meta.ref.number} --repo {repo}")
    raise RuntimeError(
        f"stale review snapshot: requested --head {requested_head}, current head is {current_head}. "
        f"Rerun `{refresh_cmd}` to refresh the snapshot."
    )


def _resolve_1_based_index_selection(*, raw: str | None, total_items: int, item_name: str) -> list[int] | None:
    if raw is None:
        return None
    if total_items < 1:
        raise RuntimeError(f"no {item_name}s are available for selection")
    selected: set[int] = set()
    for token in [chunk.strip() for chunk in raw.split(",") if chunk.strip()]:
        if "-" in token:
            left, right = token.split("-", 1)
            try:
                start = int(left)
                end = int(right)
            except ValueError as error:
                raise RuntimeError(f"invalid {item_name} range: {token}") from error
            if start < 1 or end < 1 or start > end:
                raise RuntimeError(f"invalid {item_name} range: {token}")
            selected.update(range(start, end + 1))
            continue
        try:
            value = int(token)
        except ValueError as error:
            raise RuntimeError(f"invalid {item_name} index: {token}") from error
        if value < 1:
            raise RuntimeError(f"invalid {item_name} index: {token}")
        selected.add(value)
    ordered = sorted(selected)
    if not ordered:
        raise RuntimeError(f"`--{item_name}s` did not select any {item_name}")
    if ordered[-1] > total_items:
        raise RuntimeError(f"invalid {item_name} index {ordered[-1]}, expected in 1..{total_items}")
    return ordered


def _resolve_review_start_file_indexes(*, raw: str | None, total_files: int) -> list[int]:
    indexes = _resolve_1_based_index_selection(raw=raw, total_items=total_files, item_name="file")
    if indexes is None:
        raise RuntimeError("`--files` did not select any file")
    return indexes


def _resolve_review_start_hunk_indexes(*, raw: str | None, total_hunks: int) -> list[int] | None:
    return _resolve_1_based_index_selection(raw=raw, total_items=total_hunks, item_name="hunk")


def _format_review_file_status(file: PullRequestDiffFile) -> str:
    return f"{file.status} (+{file.additions} -{file.deletions}, {file.changes} changes)"


def _load_review_start_file_snapshot_lines(
    *,
    client: GitHubClient,
    meta: PullRequestMeta,
    file: PullRequestDiffFile,
    context_lines: int,
) -> tuple[str, ...] | None:
    if context_lines <= 0:
        return None
    if file.status == "removed":
        base_ref = meta.base_ref_oid
        target_path = file.previous_path or file.path
        if base_ref is None:
            return None
        return client.fetch_file_lines(meta.ref, path=target_path, revision=base_ref)
    if file.status == "added":
        return None
    head_ref = meta.head_ref_oid
    if head_ref is None:
        return None
    return client.fetch_file_lines(meta.ref, path=file.path, revision=head_ref)


def _build_review_start_hunk_context_lines(
    *,
    hunk: _DiffHunk,
    file: PullRequestDiffFile,
    snapshot_lines: tuple[str, ...] | None,
    context_lines: int,
) -> tuple[list[tuple[int | None, int | None, str]], list[tuple[int | None, int | None, str]]] | None:
    if context_lines <= 0 or snapshot_lines is None:
        return None

    match = _HUNK_HEADER_RE.match(hunk.header)
    if match is None:
        return None
    old_start = int(match.group("old"))
    new_start = int(match.group("new"))
    old_cursor, new_cursor = _resolve_hunk_end_cursors(hunk, old_start=old_start, new_start=new_start)

    if file.status == "removed":
        return _build_left_only_review_context_lines(
            snapshot_lines=snapshot_lines,
            old_start=old_start,
            old_cursor=old_cursor,
            context_lines=context_lines,
        )
    if file.status == "added":
        return None
    return _build_both_side_review_context_lines(
        snapshot_lines=snapshot_lines,
        old_start=old_start,
        new_start=new_start,
        old_cursor=old_cursor,
        new_cursor=new_cursor,
        context_lines=context_lines,
    )


def _resolve_hunk_end_cursors(hunk: _DiffHunk, *, old_start: int, new_start: int) -> tuple[int, int]:
    old_line = old_start
    new_line = new_start
    for raw in hunk.lines[1:]:
        marker = raw[:1]
        if marker == "+":
            new_line += 1
        elif marker == " ":
            old_line += 1
            new_line += 1
        elif marker == "-":
            old_line += 1
    return old_line, new_line


def _build_both_side_review_context_lines(
    *,
    snapshot_lines: tuple[str, ...],
    old_start: int,
    new_start: int,
    old_cursor: int,
    new_cursor: int,
    context_lines: int,
) -> tuple[list[tuple[int | None, int | None, str]], list[tuple[int | None, int | None, str]]]:
    leading_count = min(context_lines, old_start - 1, new_start - 1)
    trailing_count = min(context_lines, max(0, len(snapshot_lines) - new_cursor + 1))
    leading: list[tuple[int | None, int | None, str]] = [
        (
            old_start - leading_count + offset,
            new_start - leading_count + offset,
            f" {snapshot_lines[new_start - leading_count + offset - 1]}",
        )
        for offset in range(leading_count)
    ]
    trailing: list[tuple[int | None, int | None, str]] = [
        (
            old_cursor + offset,
            new_cursor + offset,
            f" {snapshot_lines[new_cursor + offset - 1]}",
        )
        for offset in range(trailing_count)
    ]
    return leading, trailing


def _resolve_nearby_thread_context_lines(
    *,
    hunk: _DiffHunk,
    summaries: list[ReviewThreadSummary],
    max_auto_context_lines: int = DEFAULT_NEARBY_THREAD_AUTO_CONTEXT_LINES,
) -> int:
    if max_auto_context_lines <= 0:
        return 0
    match = _HUNK_HEADER_RE.match(hunk.header)
    if match is None:
        return 0
    old_start = int(match.group("old"))
    new_start = int(match.group("new"))
    old_cursor, new_cursor = _resolve_hunk_end_cursors(hunk, old_start=old_start, new_start=new_start)
    old_end = old_cursor - 1
    new_end = new_cursor - 1
    rendered_line_keys = _collect_rendered_hunk_line_keys(hunk)
    required = 0
    for summary in summaries:
        anchor_key = _resolve_review_thread_display_anchor_key(summary)
        if anchor_key is None or anchor_key in rendered_line_keys:
            continue
        side, line = anchor_key
        distance = _resolve_nearby_thread_line_distance(
            side=side,
            line=line,
            old_start=old_start,
            old_end=old_end,
            new_start=new_start,
            new_end=new_end,
        )
        if distance is None or distance > max_auto_context_lines:
            continue
        required = max(required, distance)
    return required


def _resolve_nearby_thread_line_distance(
    *,
    side: str,
    line: int,
    old_start: int,
    old_end: int,
    new_start: int,
    new_end: int,
) -> int | None:
    if side == "RIGHT":
        if line < new_start:
            return new_start - line
        if line > new_end:
            return line - new_end
        return 0
    if side == "LEFT":
        if line < old_start:
            return old_start - line
        if line > old_end:
            return line - old_end
        return 0
    return None


def _build_left_only_review_context_lines(
    *,
    snapshot_lines: tuple[str, ...],
    old_start: int,
    old_cursor: int,
    context_lines: int,
) -> tuple[list[tuple[int | None, int | None, str]], list[tuple[int | None, int | None, str]]]:
    leading_count = min(context_lines, old_start - 1)
    trailing_count = min(context_lines, max(0, len(snapshot_lines) - old_cursor + 1))
    leading: list[tuple[int | None, int | None, str]] = [
        (
            old_start - leading_count + offset,
            None,
            f" {snapshot_lines[old_start - leading_count + offset - 1]}",
        )
        for offset in range(leading_count)
    ]
    trailing: list[tuple[int | None, int | None, str]] = [
        (
            old_cursor + offset,
            None,
            f" {snapshot_lines[old_cursor + offset - 1]}",
        )
        for offset in range(trailing_count)
    ]
    return leading, trailing


def _group_review_thread_summaries_by_path(
    summaries: tuple[ReviewThreadSummary, ...],
) -> dict[str, list[ReviewThreadSummary]]:
    grouped: dict[str, list[ReviewThreadSummary]] = {}
    for summary in summaries:
        grouped.setdefault(summary.path, []).append(summary)
    return grouped


def _resolve_review_start_thread_summaries_for_file(
    *,
    file: PullRequestDiffFile,
    summaries_by_path: dict[str, list[ReviewThreadSummary]],
) -> list[ReviewThreadSummary]:
    candidates: dict[str, ReviewThreadSummary] = {}
    for path in {file.path, file.previous_path}:
        if not path:
            continue
        for summary in summaries_by_path.get(path, []):
            candidates[summary.thread_id] = summary
    return sorted(candidates.values(), key=lambda summary: (summary.display_ref or "", summary.thread_id))


def _filter_review_start_thread_summaries(summaries: list[ReviewThreadSummary]) -> list[ReviewThreadSummary]:
    return [summary for summary in summaries if not _should_hide_review_start_thread(summary)]


def _should_hide_review_start_thread(summary: ReviewThreadSummary) -> bool:
    if not summary.is_outdated:
        return False
    return not _review_thread_has_current_anchor(summary)


def _review_thread_has_current_anchor(summary: ReviewThreadSummary) -> bool:
    return summary.anchor_side == "RIGHT" or bool(summary.right_lines)


def _format_review_thread_file_summary(summaries: list[ReviewThreadSummary]) -> str:
    active_count = sum(1 for summary in summaries if not summary.is_resolved)
    resolved_count = len(summaries) - active_count
    if resolved_count > 0:
        return (
            f"Existing review threads in this file: {len(summaries)} ({active_count} active, {resolved_count} resolved)"
        )
    return f"Existing review threads in this file: {len(summaries)} ({active_count} active)"


def _is_file_scoped_thread_summary(summary: ReviewThreadSummary) -> bool:
    return not summary.left_lines and not summary.right_lines


def _render_review_thread_summary_items(summaries: list[ReviewThreadSummary]) -> list[str]:
    lines: list[str] = []
    for summary in summaries:
        lines.append(f"- {_format_review_thread_summary_item(summary)}")
        lines.extend(_render_review_comment_summary_items(summary))
    return lines


def _format_review_thread_summary_item(summary: ReviewThreadSummary) -> str:
    prefix = "resolved thread" if summary.is_resolved else "thread"
    location = summary.display_ref or "file-scoped"
    comment_label = "comment" if summary.comment_count == 1 else "comments"
    return f"{prefix} {summary.thread_id} at {location} ({summary.comment_count} {comment_label})"


def _render_review_comment_summary_items(summary: ReviewThreadSummary) -> list[str]:
    lines: list[str] = []
    for index, comment in enumerate(summary.comments, start=1):
        flags: list[str] = []
        if comment.is_outdated:
            flags.append("outdated")
        if comment.is_minimized:
            flags.append(f"hidden:{(comment.minimized_reason or 'minimized').lower()}")
        flag_suffix = f" [{' '.join(flags)}]" if flags else ""
        lines.append(f"  [{index}] @{comment.author}{flag_suffix}: {comment.body_preview}")
    return lines


def _build_inline_review_thread_blocks_for_file(
    *,
    hunks: list[_DiffHunk],
    summaries: list[ReviewThreadSummary],
    extra_contexts: list[
        tuple[
            list[tuple[int | None, int | None, str]],
            list[tuple[int | None, int | None, str]],
        ]
        | None
    ],
) -> list[dict[tuple[str, int], list[str]]]:
    blocks_by_hunk: list[dict[tuple[str, int], list[str]]] = [{} for _ in hunks]
    primary_rendered_line_keys = [_collect_rendered_hunk_line_keys(hunk) for hunk in hunks]
    all_rendered_line_keys = [
        _collect_rendered_hunk_line_keys(hunk, extra_context_lines=extra_contexts[index])
        for index, hunk in enumerate(hunks)
    ]
    for summary in summaries:
        resolved = _resolve_inline_review_thread_target_hunk_index(
            summary=summary,
            primary_rendered_line_keys=primary_rendered_line_keys,
            all_rendered_line_keys=all_rendered_line_keys,
        )
        if resolved is None:
            continue
        hunk_index, anchor_key = resolved
        blocks_by_hunk[hunk_index].setdefault(anchor_key, []).extend(_render_inline_review_thread_block(summary))
    return blocks_by_hunk


def _resolve_inline_review_thread_target_hunk_index(
    *,
    summary: ReviewThreadSummary,
    primary_rendered_line_keys: list[set[tuple[str, int]]],
    all_rendered_line_keys: list[set[tuple[str, int]]],
) -> tuple[int, tuple[str, int]] | None:
    anchor_key = _resolve_review_thread_display_anchor_key(summary)
    if anchor_key is None:
        return None
    for index, keys in enumerate(primary_rendered_line_keys):
        if anchor_key in keys:
            return index, anchor_key
    for index, keys in enumerate(all_rendered_line_keys):
        if anchor_key in keys:
            return index, anchor_key
    return None


def _resolve_review_thread_display_anchor_key(summary: ReviewThreadSummary) -> tuple[str, int] | None:
    if summary.anchor_side is not None and summary.anchor_line is not None and summary.anchor_line > 0:
        return summary.anchor_side, summary.anchor_line
    if summary.right_lines:
        return "RIGHT", summary.right_lines[0]
    if summary.left_lines:
        return "LEFT", summary.left_lines[0]
    return None


def _collect_rendered_hunk_line_keys(
    hunk: _DiffHunk,
    *,
    extra_context_lines: tuple[
        list[tuple[int | None, int | None, str]],
        list[tuple[int | None, int | None, str]],
    ]
    | None = None,
) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    match = _HUNK_HEADER_RE.match(hunk.header)
    old_line = int(match.group("old")) if match is not None else 1
    new_line = int(match.group("new")) if match is not None else 1
    leading_extra_lines: list[tuple[int | None, int | None, str]] = []
    trailing_extra_lines: list[tuple[int | None, int | None, str]] = []
    if extra_context_lines is not None:
        leading_extra_lines, trailing_extra_lines = extra_context_lines

    def add(left: int | None, right: int | None) -> None:
        if left is not None:
            keys.add(("LEFT", left))
        if right is not None:
            keys.add(("RIGHT", right))

    for left, right, _ in leading_extra_lines:
        add(left, right)
    for raw in hunk.lines[1:]:
        marker = raw[:1]
        if marker == "+":
            add(None, new_line)
            new_line += 1
        elif marker == " ":
            add(old_line, new_line)
            old_line += 1
            new_line += 1
        elif marker == "-":
            add(old_line, None)
            old_line += 1
    for left, right, _ in trailing_extra_lines:
        add(left, right)
    return keys


def _render_inline_review_thread_block(summary: ReviewThreadSummary) -> list[str]:
    marker = "✓ resolved thread" if summary.is_resolved else "💬 thread"
    comment_label = "comment" if summary.comment_count == 1 else "comments"
    location_suffix = f" at {summary.display_ref}" if summary.display_ref else ""
    lines = [f"{marker} {summary.thread_id}{location_suffix} ({summary.comment_count} {comment_label})"]
    for index, comment in enumerate(summary.comments, start=1):
        flags: list[str] = []
        if comment.is_outdated:
            flags.append("outdated")
        if comment.is_minimized:
            flags.append(f"hidden:{(comment.minimized_reason or 'minimized').lower()}")
        flag_suffix = f" [{' '.join(flags)}]" if flags else ""
        lines.append(f"↳ [{index}] @{comment.author}{flag_suffix}: {comment.body_preview}")
    return lines


def _review_start_page_command(
    *,
    meta: PullRequestMeta,
    page: int | None,
    page_size: int,
    max_hunks: int,
    head: str | None,
    context_lines: int,
) -> str | None:
    if page is None:
        return None
    return display_command_with(
        _build_review_start_command(
            meta=meta,
            page=page,
            page_size=page_size,
            max_hunks=max_hunks,
            head=head,
            context_lines=context_lines,
            files=None,
            path=None,
            hunks=None,
        )
    )


def _build_review_start_command(
    *,
    meta: PullRequestMeta,
    page: int | None,
    page_size: int,
    max_hunks: int,
    head: str | None,
    context_lines: int,
    files: str | None,
    path: str | None,
    hunks: str | None,
) -> str:
    repo = f"{meta.ref.owner}/{meta.ref.name}"
    parts = ["pr", "review-start"]
    if files is not None:
        parts.extend(["--files", files])
    elif page is not None:
        parts.extend(["--page", str(page)])
        parts.extend(["--page-size", str(page_size)])
    parts.extend(["--max-hunks", str(max_hunks)])
    if path is not None:
        parts.extend(["--path", path])
    if hunks is not None:
        parts.extend(["--hunks", hunks])
    if head is not None:
        parts.extend(["--head", head])
    if context_lines > 0:
        parts.extend(["--context-lines", str(context_lines)])
    parts.extend(["--pr", str(meta.ref.number), "--repo", repo])
    return " ".join(parts)


def _fetch_review_start_files_by_index(
    *,
    client: GitHubClient,
    meta: PullRequestMeta,
    file_indexes: list[int],
) -> list[tuple[int, PullRequestDiffFile]]:
    fetch_page_size = 100
    wanted = set(file_indexes)
    files_by_index: dict[int, PullRequestDiffFile] = {}
    needed_pages = sorted({((index - 1) // fetch_page_size) + 1 for index in file_indexes})
    for page in needed_pages:
        diff_page = client.fetch_pr_files_page(meta, page=page, page_size=fetch_page_size)
        page_start = ((page - 1) * fetch_page_size) + 1 if diff_page.files else 0
        for offset, file in enumerate(diff_page.files):
            file_index = page_start + offset
            if file_index in wanted:
                files_by_index[file_index] = file
    missing = [index for index in file_indexes if index not in files_by_index]
    if missing:
        raise RuntimeError(f"failed to load selected files: {_format_line_spans(missing)}")
    return [(index, files_by_index[index]) for index in file_indexes]


def _review_start_file_selection_commands(
    *,
    meta: PullRequestMeta,
    file_indexes: list[int],
    max_hunks: int,
    head: str | None,
    context_lines: int,
) -> tuple[str | None, str | None]:
    if not file_indexes or not _is_contiguous_index_selection(file_indexes):
        return None, None
    total_files = meta.changed_files or 0
    width = len(file_indexes)
    start = file_indexes[0]
    end = file_indexes[-1]
    previous_cmd = None
    next_cmd = None
    if start > 1:
        previous_start = max(1, start - width)
        previous_end = start - 1
        previous_cmd = display_command_with(
            _build_review_start_command(
                meta=meta,
                page=None,
                page_size=DEFAULT_REVIEW_START_FILE_PAGE_SIZE,
                max_hunks=max_hunks,
                head=head,
                context_lines=context_lines,
                files=_format_range_selection(previous_start, previous_end),
                path=None,
                hunks=None,
            )
        )
    if total_files > 0 and end < total_files:
        next_start = end + 1
        next_end = min(total_files, end + width)
        next_cmd = display_command_with(
            _build_review_start_command(
                meta=meta,
                page=None,
                page_size=DEFAULT_REVIEW_START_FILE_PAGE_SIZE,
                max_hunks=max_hunks,
                head=head,
                context_lines=context_lines,
                files=_format_range_selection(next_start, next_end),
                path=None,
                hunks=None,
            )
        )
    return previous_cmd, next_cmd


def _is_contiguous_index_selection(indexes: list[int]) -> bool:
    if not indexes:
        return False
    expected = list(range(indexes[0], indexes[-1] + 1))
    return indexes == expected


def _format_range_selection(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _resolve_review_start_file(
    *,
    client: GitHubClient,
    meta: PullRequestMeta,
    path_filter: str,
) -> tuple[PullRequestDiffFile, PullRequestDiffPage, int]:
    page_size = 100
    total_files = meta.changed_files or 0
    total_pages = max(1, (total_files + page_size - 1) // page_size) if total_files > 0 else 1
    exact_match: tuple[PullRequestDiffFile, PullRequestDiffPage, int] | None = None
    suffix_matches: list[tuple[PullRequestDiffFile, PullRequestDiffPage, int]] = []

    for page in range(1, total_pages + 1):
        diff_page = client.fetch_pr_files_page(meta, page=page, page_size=page_size)
        file_start = ((page - 1) * page_size) + 1 if diff_page.files else 0
        for offset, file in enumerate(diff_page.files):
            file_index = file_start + offset
            if file.path == path_filter:
                exact_match = (file, diff_page, file_index)
                break
            if file.path.endswith(path_filter) or file.path.endswith(f"/{path_filter}"):
                suffix_matches.append((file, diff_page, file_index))
        if exact_match is not None:
            break

    if exact_match is not None:
        return exact_match
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    if len(suffix_matches) > 1:
        preview = ", ".join(file.path for file, _, _ in suffix_matches[:5])
        more = " ..." if len(suffix_matches) > 5 else ""
        raise RuntimeError(f"ambiguous --path {path_filter!r}; matches: {preview}{more}")
    raise RuntimeError(f"changed file not found for --path {path_filter!r}")


def _resolve_start_line(args: Any) -> int | None:
    raw = getattr(args, "start_line", None)
    if raw is None:
        return None
    return int(raw)


def _resolve_start_side(args: Any) -> str | None:
    start_line = _resolve_start_line(args)
    if start_line is None:
        return None
    raw = getattr(args, "start_side", None)
    if raw is None:
        return str(getattr(args, "side", "RIGHT"))
    return str(raw)


def _format_line_spans(lines: list[int]) -> str:
    if not lines:
        return "(none)"

    spans: list[str] = []
    start = lines[0]
    end = lines[0]
    for value in lines[1:]:
        if value == end + 1:
            end = value
            continue
        spans.append(str(start) if start == end else f"{start}-{end}")
        start = value
        end = value
    spans.append(str(start) if start == end else f"{start}-{end}")
    return ", ".join(spans)


def _collect_commentable_lines(path_hunks: list[_DiffHunk], *, side: str) -> list[int]:
    return sorted(
        {
            candidate
            for hunk in path_hunks
            for candidate in (hunk.right_commentable_lines if side == "RIGHT" else hunk.left_commentable_lines)
        }
    )


def _validate_one_review_thread_target(*, path: str, line: int, side: str, commentable_lines: list[int]) -> None:
    if line in commentable_lines:
        return

    if not commentable_lines:
        raise RuntimeError(
            f"line {line} on {side} is not a commentable diff line for {path}. "
            f"The current diff has no commentable lines on {side} for that file."
        )

    preview = ", ".join(str(candidate) for candidate in commentable_lines[:8])
    if len(commentable_lines) > 8:
        preview += ", ..."
    raise RuntimeError(
        f"line {line} on {side} is not a commentable diff line for {path}. "
        f"Try a line from the PR diff for that side instead (e.g. {preview})."
    )


def _validate_review_thread_target(
    *,
    client: GitHubClient,
    args: Any,
    path: str,
    line: int,
    side: str,
    start_line: int | None = None,
    start_side: str | None = None,
) -> None:
    diff = client.fetch_pr_diff(selector=getattr(args, "pr", None), repo=getattr(args, "repo", None))
    hunks = _extract_diff_hunks(diff)
    path_hunks = [hunk for hunk in hunks if path in hunk.match_paths]
    if not path_hunks:
        raise RuntimeError(f"path is not part of the PR diff: {path}")

    commentable_lines = _collect_commentable_lines(path_hunks, side=side)
    _validate_one_review_thread_target(path=path, line=line, side=side, commentable_lines=commentable_lines)

    if start_line is None:
        return

    resolved_start_side = side if start_side is None else start_side
    start_commentable_lines = _collect_commentable_lines(path_hunks, side=resolved_start_side)
    _validate_one_review_thread_target(
        path=path,
        line=start_line,
        side=resolved_start_side,
        commentable_lines=start_commentable_lines,
    )


def parse_event_indexes(raw_indexes: list[str]) -> list[int]:
    values: set[int] = set()
    for raw in raw_indexes:
        chunks = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        for chunk in chunks:
            if "-" in chunk:
                left, right = chunk.split("-", 1)
                start = int(left)
                end = int(right)
                if start <= 0 or end <= 0:
                    raise RuntimeError(f"invalid index range: {chunk}")
                if start > end:
                    start, end = end, start
                values.update(range(start, end + 1))
                continue
            value = int(chunk)
            if value <= 0:
                raise RuntimeError(f"invalid event index: {value}")
            values.add(value)

    if not values:
        raise RuntimeError("no valid event indexes provided")
    return sorted(values)


def parse_review_ids(raw_review_ids: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in raw_review_ids:
        for token in [chunk.strip() for chunk in raw.split(",") if chunk.strip()]:
            if not token.startswith("PRR_"):
                raise RuntimeError(f"invalid review id: {token}")
            if token in seen:
                continue
            seen.add(token)
            values.append(token)
    if not values:
        raise RuntimeError("no valid review ids provided")
    return values


def _parse_thread_range(raw: str | None) -> tuple[int, int] | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if "-" in text:
        left, right = text.split("-", 1)
    elif ".." in text:
        left, right = text.split("..", 1)
    else:
        left, right = text, text
    start = int(left)
    end = int(right)
    if start <= 0 or end <= 0:
        raise RuntimeError(f"invalid thread range: {text}")
    if start > end:
        start, end = end, start
    return start, end
