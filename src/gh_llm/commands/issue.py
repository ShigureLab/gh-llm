from __future__ import annotations

from dataclasses import dataclass
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
from gh_llm.pager import DEFAULT_PAGE_SIZE, TimelinePager, build_context_from_meta
from gh_llm.render import (
    render_comment_node_detail,
    render_description,
    render_event_detail_blocks,
    render_expand_hints,
    render_frontmatter,
    render_header,
    render_hidden_gap,
    render_issue_actions,
    render_page,
)

if TYPE_CHECKING:
    from gh_llm.models import PullRequestMeta, TimelineContext, TimelinePage


@dataclass(frozen=True)
class _ExpandOptions:
    minimized: bool = False
    details: bool = False


@dataclass(frozen=True)
class _ShowOptions:
    meta: bool = True
    description: bool = True
    timeline: bool = True
    actions: bool = True


def register_issue_parser(subparsers: Any) -> None:
    issue_parser = subparsers.add_parser("issue", help="issue reading and actions")
    issue_subparsers = issue_parser.add_subparsers(dest="issue_command")

    view_parser = issue_subparsers.add_parser(
        "view",
        help="show issue overview with timeline and actions",
    )
    view_parser.add_argument("issue", nargs="?", help="Issue number/url")
    view_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    view_parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="timeline entries per page")
    view_parser.add_argument(
        "--show",
        action="append",
        default=[],
        help="show regions: meta, description, timeline, actions, all (comma-separated or repeatable)",
    )
    view_parser.add_argument(
        "--expand",
        action="append",
        default=[],
        help="auto-expand folded content: minimized, details, all (comma-separated or repeatable)",
    )
    add_timeline_window_arguments(view_parser)
    view_parser.set_defaults(handler=cmd_issue_view)

    timeline_expand_parser = issue_subparsers.add_parser("timeline-expand", help="load one timeline page by number")
    timeline_expand_parser.add_argument("page", type=int, help="1-based page number")
    timeline_expand_parser.add_argument("--issue", help="Issue number/url")
    timeline_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    timeline_expand_parser.add_argument("--page-size", type=int, help="timeline entries per page")
    timeline_expand_parser.add_argument(
        "--expand",
        action="append",
        default=[],
        help="auto-expand folded content: minimized, details, all (comma-separated or repeatable)",
    )
    add_timeline_window_arguments(timeline_expand_parser)
    timeline_expand_parser.set_defaults(handler=cmd_issue_timeline_expand)

    details_expand_parser = issue_subparsers.add_parser(
        "details-expand",
        help="show collapsed <details>/<summary> blocks for one timeline event",
    )
    details_expand_parser.add_argument("index", type=int, help="1-based event index from timeline view")
    details_expand_parser.add_argument("--issue", help="Issue number/url")
    details_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    details_expand_parser.add_argument("--page-size", type=int, help="timeline entries per page")
    add_timeline_window_arguments(details_expand_parser)
    details_expand_parser.set_defaults(handler=cmd_issue_details_expand)

    comment_edit_parser = issue_subparsers.add_parser("comment-edit", help="edit one issue comment by node id")
    comment_edit_parser.add_argument("comment_id", help="comment id, e.g. IC_xxx")
    add_body_input_arguments(
        comment_edit_parser,
        required=True,
        body_help="new comment body",
        file_help="read new comment body from file (use `-` to read from standard input)",
    )
    comment_edit_parser.add_argument("--issue", help="Issue number/url")
    comment_edit_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    comment_edit_parser.set_defaults(handler=cmd_issue_comment_edit)

    comment_expand_parser = issue_subparsers.add_parser("comment-expand", help="expand one comment by node id")
    comment_expand_parser.add_argument("comment_id", help="comment id, e.g. IC_xxx")
    comment_expand_parser.add_argument("--issue", help="Issue number/url")
    comment_expand_parser.add_argument("--repo", help="repository in OWNER/REPO format")
    comment_expand_parser.set_defaults(handler=cmd_issue_comment_expand)


def cmd_issue_view(args: Any) -> int:
    page_size = int(args.page_size)
    expand = _parse_expand_options(raw_values=list(getattr(args, "expand", [])))
    show = _parse_show_options(raw_values=list(getattr(args, "show", [])))
    timeline_window = _resolve_timeline_window(args)
    client = GitHubClient()
    pager = TimelinePager(client)

    meta = _resolve_issue_meta(client=client, args=args)
    context = build_context_from_meta(meta=meta, page_size=page_size)
    first_page: TimelinePage | None = None
    last_page: TimelinePage | None = None
    shown_pages: set[int] = set()

    if show.timeline:
        context, first_page, last_page = pager.build_initial(
            meta,
            page_size=page_size,
            timeline_window=timeline_window,
            show_minimized_details=expand.minimized,
            show_details_blocks=expand.details,
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
                show_minimized_details=expand.minimized,
                show_details_blocks=expand.details,
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
    if show.actions:
        print_block(render_issue_actions(context))

    return 0


def cmd_issue_timeline_expand(args: Any) -> int:
    client = GitHubClient()
    pager = TimelinePager(client)
    expand = _parse_expand_options(raw_values=list(getattr(args, "expand", [])))
    context, meta = _resolve_context_and_meta(
        client=client,
        pager=pager,
        args=args,
        show_minimized_details=expand.minimized,
        show_details_blocks=expand.details,
    )

    page = pager.fetch_page(
        meta=meta,
        context=context,
        page=int(args.page),
        show_minimized_details=expand.minimized,
        show_details_blocks=expand.details,
    )

    for line in render_header(context):
        print(line)
    print("## Timeline")
    for line in render_page(int(args.page), context, page):
        print(line)

    return 0


def cmd_issue_details_expand(args: Any) -> int:
    client = GitHubClient()
    pager = TimelinePager(client)
    context, meta = _resolve_context_and_meta(
        client=client,
        pager=pager,
        args=args,
        show_minimized_details=True,
        show_details_blocks=True,
    )

    index = int(args.index)
    page_number = _resolve_timeline_page_for_index(context=context, index=index)
    if page_number is None:
        raise RuntimeError(f"invalid event index {index}, expected in 1..{context.total_count}")

    page = pager.fetch_page(
        meta=meta,
        context=context,
        page=page_number,
        show_minimized_details=True,
        show_details_blocks=True,
        diff_hunk_lines=None,
    )
    try:
        offset = page.absolute_indexes.index(index)
    except ValueError:
        raise RuntimeError("event index is outside loaded page range") from None
    except AttributeError as error:  # pragma: no cover - defensive fallback
        raise RuntimeError("event index is outside loaded page range") from error

    for line in render_event_detail_blocks(index=index, event=page.items[offset]):
        print(line)
    return 0


def cmd_issue_comment_edit(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_issue(client=client, args=args)
    updated_comment_id = client.edit_comment(
        comment_id=str(args.comment_id),
        body=resolve_file_or_inline_text(args, text_attr="body", file_attr="body_file"),
    )
    print(f"comment: {updated_comment_id}")
    print("status: edited")
    return 0


def cmd_issue_comment_expand(args: Any) -> int:
    client = GitHubClient()
    _resolve_optional_issue(client=client, args=args)
    node = client.fetch_comment_node(str(args.comment_id))
    for line in render_comment_node_detail(str(args.comment_id), node):
        print(line)
    return 0


def _resolve_issue_meta(*, client: GitHubClient, args: Any) -> PullRequestMeta:
    return resolve_subject(
        selector=getattr(args, "issue", None),
        repo=getattr(args, "repo", None),
        selector_flag="--issue",
        resolver=client.resolve_issue,
    )


def _resolve_optional_issue(*, client: GitHubClient, args: Any) -> PullRequestMeta | None:
    return maybe_resolve_subject(
        selector=getattr(args, "issue", None),
        repo=getattr(args, "repo", None),
        selector_flag="--issue",
        resolver=client.resolve_issue,
    )


def _resolve_context_and_meta(
    *,
    client: GitHubClient,
    pager: TimelinePager,
    args: Any,
    show_minimized_details: bool = False,
    show_details_blocks: bool = False,
) -> tuple[TimelineContext, PullRequestMeta]:
    page_size = getattr(args, "page_size", None)
    effective_page_size = DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
    meta = _resolve_issue_meta(client=client, args=args)
    context, _, _ = pager.build_initial(
        meta=meta,
        page_size=effective_page_size,
        timeline_window=_resolve_timeline_window(args),
        show_minimized_details=show_minimized_details,
        show_details_blocks=show_details_blocks,
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


def _parse_expand_options(*, raw_values: list[str]) -> _ExpandOptions:
    minimized = False
    details = False

    aliases: dict[str, str] = {
        "minimized": "minimized",
        "details": "details",
        "detail": "details",
        "all": "all",
        "*": "all",
    }
    valid_values = ["minimized", "details", "all"]
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
                minimized = True
                details = True
                continue
            if normalized == "minimized":
                minimized = True
            elif normalized == "details":
                details = True

    return _ExpandOptions(minimized=minimized, details=details)


def _parse_show_options(*, raw_values: list[str]) -> _ShowOptions:
    if not raw_values:
        return _ShowOptions()

    selected: set[str] = set()
    aliases: dict[str, set[str]] = {
        "meta": {"meta"},
        "description": {"description"},
        "desc": {"description"},
        "timeline": {"timeline"},
        "actions": {"actions"},
        "summary": {"meta", "description"},
        "all": {"meta", "description", "timeline", "actions"},
        "*": {"meta", "description", "timeline", "actions"},
    }
    valid_values = ["meta", "description", "timeline", "actions", "summary", "all"]
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
        actions=("actions" in selected),
    )
