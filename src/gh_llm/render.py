from __future__ import annotations

import json
import re
from datetime import UTC
from typing import TYPE_CHECKING, cast

from gh_llm.invocation import display_command, display_command_with

if TYPE_CHECKING:
    from string.templatelib import Template

    from gh_llm.models import CheckItem, TimelineContext, TimelineEvent, TimelinePage


DETAILS_BLOCK_RE = re.compile(r"(?is)<details\b[^>]*>(.*?)</details>")
SUMMARY_RE = re.compile(r"(?is)<summary\b[^>]*>(.*?)</summary>")


def render_header(context: TimelineContext) -> list[str]:
    return [*render_frontmatter(context), "", *render_description(context)]


def render_frontmatter(context: TimelineContext) -> list[str]:
    is_issue = context.kind == "issue"
    key = "issue" if is_issue else "pr"
    lines = [
        "---",
        f"{key}: {context.number}",
        f"repo: {context.owner}/{context.name}",
        f"title: {json.dumps(context.title, ensure_ascii=False)}",
        f"url: {context.url}",
        f"author: {context.author}",
        f"state: {context.state}",
        f"labels: {json.dumps(list(context.labels), ensure_ascii=False)}",
        f"draft: {str(context.is_draft).lower()}",
        f"updated_at: {context.updated_at}",
        f"fetched_at: {context.fetched_at}",
    ]
    if context.timeline_loaded:
        lines.extend(
            [
                f"timeline_events: {context.total_count}",
                *(
                    [f"timeline_events_unfiltered: {context.timeline_unfiltered_count}"]
                    if context.timeline_filtered
                    else []
                ),
                *([f"timeline_after: {context.timeline_after}"] if context.timeline_after else []),
                *([f"timeline_before: {context.timeline_before}"] if context.timeline_before else []),
                f"page_size: {context.page_size}",
                f"total_pages: {context.total_pages}",
            ]
        )
    if context.kind == "pr":
        lines.append(f"is_merged: {str(context.is_merged).lower()}")
        if context.head_ref_repo:
            lines.append(f"head_ref_repo: {context.head_ref_repo}")
        if context.head_ref_name:
            lines.append(f"head_ref_name: {context.head_ref_name}")
        if context.head_ref_oid:
            lines.append(f"head_ref_oid: {context.head_ref_oid}")
        if context.head_ref_deleted is not None:
            lines.append(f"head_ref_deleted: {str(context.head_ref_deleted).lower()}")
    lines.append("---")
    return lines


def render_diff_actions(context: TimelineContext) -> list[str]:
    if context.kind == "issue":
        return []
    repo = f"{context.owner}/{context.name}"
    return [f"Δ PR diff: `gh pr diff {context.number} --repo {repo}`"]


def render_description(context: TimelineContext) -> list[str]:
    description = context.body.strip() or "(no description)"
    repo = f"{context.owner}/{context.name}"
    is_issue = context.kind == "issue"
    noun_title = "Issue" if is_issue else "PR"
    description_tag = "description"
    description_title = "## Description"
    edit_cmd = (
        f"gh issue edit {context.number} --repo {repo} --body '<issue_description_markdown>'"
        if is_issue
        else f"gh pr edit {context.number} --repo {repo} --body '<pr_description_markdown>'"
    )
    body_placeholder = (
        "⌨ issue_body: '<issue_description_markdown>'" if is_issue else "⌨ pr_body: '<pr_description_markdown>'"
    )
    return [
        description_title,
        *([f"Reactions: {context.pr_reactions_summary}"] if context.pr_reactions_summary else []),
        *(
            [
                body_placeholder,
                f"⏎ Edit {noun_title} description via gh: `{edit_cmd}`",
            ]
            if context.can_edit_pr_body
            else []
        ),
        f"<{description_tag}>",
        *description.splitlines(),
        f"</{description_tag}>",
        "",
    ]


def render_page(page_number: int, context: TimelineContext, page: TimelinePage) -> list[str]:
    lines = [f"### Page {page_number}/{context.total_pages}"]
    if not page.items:
        lines.append("(no events on this page)")
        return lines

    for index, item in zip(
        _page_indexes(page_number=page_number, context=context, page=page), page.items, strict=False
    ):
        lines.extend(_render_item(index=index, event=item, context=context, command_group=context.kind))
    return lines


def render_expand_hints(context: TimelineContext, shown_pages: set[int]) -> list[str]:
    hidden_pages = [page for page in range(1, context.total_pages + 1) if page not in shown_pages]
    if not hidden_pages:
        if context.total_pages == 1:
            return ["Timeline fits on a single page."]
        return ["All timeline pages are already shown."]
    return []


def render_pr_actions(context: TimelineContext, *, include_diff: bool = True, include_manage: bool = True) -> list[str]:
    repo = f"{context.owner}/{context.name}"
    lines = ["## Actions"]
    if include_diff:
        lines.extend(render_diff_actions(context))
    if include_manage:
        close_or_reopen_lines: list[str] = []
        if context.state == "OPEN":
            close_or_reopen_lines.append(f"⏎ Close PR via gh: `gh pr close {context.number} --repo {repo}`")
        elif context.state == "CLOSED" and not context.is_merged:
            close_or_reopen_lines.append(f"⏎ Reopen PR via gh: `gh pr reopen {context.number} --repo {repo}`")

        branch_lines: list[str] = []
        if context.state in {"CLOSED", "MERGED"}:
            if (
                context.head_ref_deleted is True
                and context.head_ref_repo
                and context.head_ref_name
                and context.head_ref_oid
            ):
                branch_lines.append(
                    "⏎ Restore head branch via gh: "
                    f"`gh api repos/{context.head_ref_repo}/git/refs -X POST -f ref='refs/heads/{context.head_ref_name}' -f sha={context.head_ref_oid}`"
                )
            elif context.head_ref_deleted is False and context.head_ref_name and context.head_ref_repo:
                branch_lines.append(
                    f"⏎ Delete head branch via gh: `gh api -X DELETE repos/{context.head_ref_repo}/git/refs/heads/{context.head_ref_name}`"
                )

        lines.extend(
            [
                "⌨ comment_body: '<comment_body>'",
                f"⏎ Comment via gh: `gh pr comment {context.number} --repo {repo} --body '<comment_body>'`",
                "⌨ comment_body_file: '<path-or->'",
                f"⏎ Multi-line comment via gh: `gh pr comment {context.number} --repo {repo} --body-file <path-or->`",
                *close_or_reopen_lines,
                "⌨ labels_csv: '<label1>,<label2>'",
                f"⏎ Add labels via gh: `gh pr edit {context.number} --repo {repo} --add-label '<label1>,<label2>'`",
                f"⏎ Remove labels via gh: `gh pr edit {context.number} --repo {repo} --remove-label '<label1>,<label2>'`",
                "⌨ reviewers_csv: '<reviewer1>,<reviewer2>'",
                f"⏎ Request review via gh: `gh pr edit {context.number} --repo {repo} --add-reviewer '<reviewer1>,<reviewer2>'`",
                "⌨ assignees_csv: '<assignee1>,<assignee2>'",
                f"⏎ Assign via gh: `gh pr edit {context.number} --repo {repo} --add-assignee '<assignee1>,<assignee2>'`",
                *branch_lines,
            ]
        )
    return lines


def render_issue_actions(context: TimelineContext) -> list[str]:
    repo = f"{context.owner}/{context.name}"
    close_or_reopen_lines: list[str] = []
    if context.state == "OPEN":
        close_or_reopen_lines.append(f"⏎ Close issue via gh: `gh issue close {context.number} --repo {repo}`")
    elif context.state == "CLOSED":
        close_or_reopen_lines.append(f"⏎ Reopen issue via gh: `gh issue reopen {context.number} --repo {repo}`")
    return [
        "## Actions",
        "⌨ comment_body: '<comment_body>'",
        f"⏎ Comment via gh: `gh issue comment {context.number} --repo {repo} --body '<comment_body>'`",
        "⌨ comment_body_file: '<path-or->'",
        f"⏎ Multi-line comment via gh: `gh issue comment {context.number} --repo {repo} --body-file <path-or->`",
        *close_or_reopen_lines,
        "⌨ labels_csv: '<label1>,<label2>'",
        f"⏎ Add labels via gh: `gh issue edit {context.number} --repo {repo} --add-label '<label1>,<label2>'`",
        f"⏎ Remove labels via gh: `gh issue edit {context.number} --repo {repo} --remove-label '<label1>,<label2>'`",
        "⌨ assignees_csv: '<assignee1>,<assignee2>'",
        f"⏎ Assign via gh: `gh issue edit {context.number} --repo {repo} --add-assignee '<assignee1>,<assignee2>'`",
    ]


def render_checks_section(
    *,
    context: TimelineContext,
    checks: list[CheckItem],
    show_all: bool,
    is_open: bool,
) -> list[str]:
    repo = f"{context.owner}/{context.name}"
    if not is_open:
        return [
            "## Checks",
            f"Closed PR: checks are hidden by default. ⏎ run `{display_command_with(f'pr checks --pr {context.number} --repo {repo} --all')}`",
            "",
        ]

    visible = checks if show_all else [item for item in checks if not item.passed]
    hidden_count = max(0, len(checks) - len(visible))
    lines = ["## Checks"]
    if not visible:
        if checks:
            lines.append("All checks passed.")
        else:
            lines.append("(no checks found)")
    else:
        for idx, item in enumerate(visible, start=1):
            lines.append(f"{idx}. [{item.status}] {item.name} ({item.kind})")
            if item.run_id is not None:
                if item.job_id is not None:
                    lines.append(f"   ⏎ details: `gh run view {item.run_id} --job {item.job_id} --repo {repo}`")
                    lines.append(f"   ⏎ logs: `gh run view {item.run_id} --job {item.job_id} --log --repo {repo}`")
                else:
                    lines.append(f"   ⏎ details: `gh run view {item.run_id} --repo {repo}`")
                    lines.append(f"   ⏎ logs: `gh run view {item.run_id} --log --repo {repo}`")
            elif item.details_url:
                lines.append(f"   ⏎ details: `{item.details_url}`")
    if show_all:
        lines.append(
            f"⏎ show only non-passed: `{display_command_with(f'pr checks --pr {context.number} --repo {repo}')}`"
        )
    elif hidden_count > 0:
        lines.append(
            f"{hidden_count} passed checks hidden. ⏎ show all: `{display_command_with(f'pr checks --pr {context.number} --repo {repo} --all')}`"
        )
    else:
        lines.append(f"⏎ show all: `{display_command_with(f'pr checks --pr {context.number} --repo {repo} --all')}`")
    lines.append("")
    return lines


def render_mergeability_section(*, context: TimelineContext, checks: list[CheckItem]) -> list[str]:
    lines = ["## Mergeability"]
    repo = f"{context.owner}/{context.name}"
    if context.is_merged or context.state == "MERGED":
        lines.append("Status: Already merged")
        details: list[str] = []
        merge_state = (context.merge_state_status or "").upper()
        mergeable = (context.mergeable or "").upper()
        review_decision = (context.review_decision or "").upper()
        if merge_state:
            details.append(f"merge_state: {merge_state}")
        if mergeable:
            details.append(f"mergeable: {mergeable}")
        if review_decision:
            details.append(f"review_decision: {review_decision}")
        if details:
            lines.append("Details: " + ", ".join(details))
        lines.append("")
        return lines

    merge_state = (context.merge_state_status or "").upper()
    mergeable = (context.mergeable or "").upper()
    review_decision = (context.review_decision or "").upper()

    blockers: list[str] = []
    has_merge_conflict = merge_state == "DIRTY" or mergeable == "CONFLICTING"
    if has_merge_conflict:
        blockers.append("Merge conflicts detected.")

    failed_checks = [
        item.name for item in checks if not item.passed and ("FAIL" in item.status or "ERROR" in item.status)
    ]
    pending_checks = [item.name for item in checks if not item.passed and item.name not in failed_checks]
    if context.requires_status_checks:
        if failed_checks:
            blockers.append(
                "Required checks may be failing: "
                + ", ".join(failed_checks[:6])
                + ("..." if len(failed_checks) > 6 else "")
            )
        elif pending_checks:
            blockers.append(
                "Required checks may still be pending: "
                + ", ".join(pending_checks[:6])
                + ("..." if len(pending_checks) > 6 else "")
            )

    if review_decision == "CHANGES_REQUESTED":
        blockers.append("Changes requested by reviewers.")
    elif review_decision == "REVIEW_REQUIRED":
        blockers.append("Required approvals not met.")

    if context.requires_approving_reviews and context.required_approving_review_count is not None:
        approved = context.approved_review_count if context.approved_review_count is not None else 0
        required = context.required_approving_review_count
        if approved < required:
            blockers.append(f"Approvals not enough: {approved}/{required}.")

    if context.requires_code_owner_reviews and review_decision in {"REVIEW_REQUIRED", "CHANGES_REQUESTED", ""}:
        blockers.append("Code owner review may be required but not satisfied.")

    if blockers:
        lines.append("Status: Merging is blocked")
        lines.append("Reasons:")
        for idx, reason in enumerate(blockers, start=1):
            lines.append(f"{idx}. {reason}")
        if has_merge_conflict and context.conflict_files:
            lines.append("Conflicted files:")
            max_show = 20
            for path in context.conflict_files[:max_show]:
                lines.append(f"- `{path}`")
            if len(context.conflict_files) > max_show:
                lines.append(f"- ... {len(context.conflict_files) - max_show} more files")
        elif has_merge_conflict:
            lines.append(
                f"⏎ run `{display_command_with(f'pr conflict-files --pr {context.number} --repo {repo}')}` to detect conflicted files"
            )
    else:
        lines.append("Status: Merging is allowed")

    if context.state == "OPEN" and not blockers:
        merge_subject = f"{context.title} (#{context.number})"
        merge_subject_quoted = _shell_single_quote(merge_subject)
        available_methods = [
            ("merge", context.merge_commit_allowed),
            ("squash", context.squash_merge_allowed),
            ("rebase", context.rebase_merge_allowed),
        ]
        enabled_methods = [method for method, enabled in available_methods if enabled is True]
        disabled_methods = [method for method, enabled in available_methods if enabled is False]
        if enabled_methods:
            lines.append("Merge actions:")
            lines.append(f"⌨ merge_subject: '{merge_subject}'")
            if context.co_author_trailers:
                lines.append("⌨ merge_body (default):")
                lines.append("   <optional_merge_body>")
                lines.append("")
                lines.extend(f"   {trailer}" for trailer in context.co_author_trailers)
            else:
                lines.append("⌨ merge_body: '<optional_merge_body>'")
            for method in enabled_methods:
                if method == "rebase":
                    lines.append(f"⏎ rebase via gh: `gh pr merge {context.number} --repo {repo} --rebase`")
                    continue
                lines.append(
                    f"⏎ {method} via gh: `gh pr merge {context.number} --repo {repo} --{method} --subject {merge_subject_quoted} --body '<merge_body>'`"
                )
        if disabled_methods:
            lines.append(f"Disabled by repository settings: {', '.join(disabled_methods)}")

    details: list[str] = []
    if merge_state:
        details.append(f"merge_state: {merge_state}")
    if mergeable:
        details.append(f"mergeable: {mergeable}")
    if review_decision:
        details.append(f"review_decision: {review_decision}")
    if context.required_approving_review_count is not None:
        approved = context.approved_review_count if context.approved_review_count is not None else 0
        details.append(f"approvals: {approved}/{context.required_approving_review_count}")
    if details:
        lines.append("Details: " + ", ".join(details))
    lines.append("")
    return lines


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_hidden_gap(context: TimelineContext, hidden_pages: list[int]) -> list[str]:
    if not hidden_pages:
        return []
    repo = f"{context.owner}/{context.name}"
    selector_name = "issue" if context.kind == "issue" else "pr"
    filter_flags = _timeline_filter_flags(context)
    hidden_label = (
        f"Hidden timeline page: {hidden_pages[0]}"
        if len(hidden_pages) == 1
        else f"Hidden timeline pages: {hidden_pages[0]}..{hidden_pages[-1]}"
    )
    return [
        "---",
        hidden_label,
        *[
            _render_template(
                t"- ⏎ `{display_command_with(f'{context.kind} timeline-expand {page} --{selector_name} {context.number} --repo {repo}{filter_flags}')}`"
            )
            for page in hidden_pages
        ],
        "---",
    ]


def _render_item(index: int, event: TimelineEvent, context: TimelineContext, command_group: str) -> list[str]:
    timestamp = event.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    selector_name = "issue" if command_group == "issue" else "pr"
    filter_flags = _timeline_filter_flags(context)
    details_expand_cmd = display_command_with(
        f"{command_group} details-expand {index} --{selector_name} {context.number} --repo {context.owner}/{context.name}{filter_flags}"
    )
    details_action = f"⏎ run `{details_expand_cmd}`"
    display_summary = (event.summary or "").replace(
        "(details body collapsed)",
        f"(details body collapsed; {details_action})",
    )
    lines = [f"{index}. [{timestamp}] {event.kind} by @{event.actor}"]
    if event.kind == "comment":
        lines.append("   Comment:")
        lines.extend(_indented_tag_block("comment", display_summary, indent="   "))
        if event.reactions_summary:
            lines.append(f"   Reactions: {event.reactions_summary}")
        if event.editable_comment_id:
            edit_cmd = display_command_with(
                f"{command_group} comment-edit {event.editable_comment_id} --body '<comment_body>' --{selector_name} {context.number} --repo {context.owner}/{context.name}"
            )
            edit_file_cmd = display_command_with(
                f"{command_group} comment-edit {event.editable_comment_id} --body-file <comment.md> --{selector_name} {context.number} --repo {context.owner}/{context.name}"
            )
            lines.append(f"   ◌ comment_id: {event.editable_comment_id}")
            lines.append("   ⌨ comment_body: '<comment_body>'")
            lines.append("   ⌨ comment_body_file: '<comment.md>'")
            lines.append(f"   ⏎ Edit comment via {display_command()}: `{edit_cmd}`")
            lines.append(f"   ⏎ Multi-line edit via {display_command()}: `{edit_file_cmd}`")
    else:
        lines.extend(_indent_block(display_summary))
    if event.resolved_hidden_count > 0:
        repo = f"{context.owner}/{context.name}"
        lines.append(
            f"   {event.resolved_hidden_count} resolved review comments are collapsed; "
            f"⏎ run `{display_command_with(f'pr review-expand {event.source_id} --pr {context.number} --repo {repo}')}`"
        )
    if event.kind.startswith("review/"):
        detail_text = (event.full_text or event.summary or "").lower()
        if "diff hunk clipped" in detail_text:
            lines.append(
                f"   ⏎ run `{display_command_with(f'pr review-expand {event.source_id} --pr {context.number} --repo {context.owner}/{context.name} --diff-hunk-lines 0')}` for full diff hunk context"
            )
    if event.is_truncated:
        if event.kind == "comment":
            lines.append(
                f"   ⏎ run `{display_command_with(f'{command_group} comment-expand {event.source_id} --{selector_name} {context.number} --repo {context.owner}/{context.name}')}` for full comment"
            )
    if event.kind == "push/commit":
        lines.append(
            f"   Δ commit diff: `gh api repos/{context.owner}/{context.name}/commits/{event.source_id} -H 'Accept: application/vnd.github.v3.diff'`"
        )
    return lines


def _render_template(template: Template) -> str:
    rendered: list[str] = []
    for index, segment in enumerate(template.strings):
        rendered.append(segment)
        if index < len(template.interpolations):
            rendered.append(str(template.interpolations[index].value))
    return "".join(rendered)


def _page_indexes(page_number: int, context: TimelineContext, page: TimelinePage) -> tuple[int, ...]:
    if page.absolute_indexes:
        return page.absolute_indexes
    start_index = _page_start_index(page_number=page_number, context=context, page=page)
    return tuple(range(start_index, start_index + len(page.items)))


def _page_start_index(page_number: int, context: TimelineContext, page: TimelinePage) -> int:
    if page_number == context.total_pages and context.total_count % context.page_size != 0:
        return max(1, context.total_count - len(page.items) + 1)
    return (page_number - 1) * context.page_size + 1


def _timeline_filter_flags(context: TimelineContext) -> str:
    parts: list[str] = []
    if context.timeline_after:
        parts.extend(["--after", context.timeline_after])
    if context.timeline_before:
        parts.extend(["--before", context.timeline_before])
    return (" " + " ".join(parts)) if parts else ""


def render_event_detail(index: int, event: TimelineEvent) -> list[str]:
    timestamp = event.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"## Timeline Event {index}",
        f"- Type: {event.kind}",
        f"- Actor: @{event.actor}",
        f"- Time: {timestamp}",
        f"- Source ID: {event.source_id}",
        "",
    ]
    detail = event.full_text or event.summary
    if event.kind == "comment":
        lines.extend(["<comment>", *detail.splitlines(), "</comment>"])
        if event.reactions_summary:
            lines.append(f"Reactions: {event.reactions_summary}")
    else:
        lines.extend(detail.splitlines() or [event.summary])
    return lines


def render_event_detail_blocks(index: int, event: TimelineEvent) -> list[str]:
    detail = event.full_text or event.summary
    blocks = _extract_details_blocks(detail)
    lines = [f"## Details Blocks for Event {index}"]
    if not blocks:
        lines.append("(no <details> blocks found)")
        return lines
    for position, (summary, body) in enumerate(blocks, start=1):
        lines.append(f"{position}.")
        lines.append("   <details>")
        lines.append(f"   <summary>{summary}</summary>")
        for raw in body.splitlines() or ["(empty)"]:
            lines.append(f"   {raw}")
        lines.append("   </details>")
    return lines


def render_comment_node_detail(comment_id: str, node: dict[str, object]) -> list[str]:
    typename = str(node.get("__typename") or "")
    actor = _render_actor(node.get("author"))
    created_at = str(node.get("createdAt") or "")
    body = str(node.get("body") or "")
    lines = [f"## Comment {comment_id}", f"- Type: {typename}", f"- Actor: @{actor}", f"- Time: {created_at}", ""]
    lines.extend(["<comment>", *(body.splitlines() or [""]), "</comment>"])
    reactions = _render_reactions(node.get("reactionGroups"))
    if reactions:
        lines.append(f"Reactions: {reactions}")
    if typename == "PullRequestReviewComment":
        path = str(node.get("path") or "(unknown path)")
        line = node.get("line")
        original_line = node.get("originalLine")
        review_obj = node.get("pullRequestReview")
        review_id = ""
        if isinstance(review_obj, dict):
            review = cast("dict[str, object]", review_obj)
            review_id = str(review.get("id") or "")
        lines.append(f"path: {path}")
        if line is not None:
            lines.append(f"line: {line}")
        if original_line is not None:
            lines.append(f"original_line: {original_line}")
        if review_id:
            lines.append(f"review_id: {review_id}")
        diff_hunk = str(node.get("diffHunk") or "").strip()
        if diff_hunk:
            lines.extend(["Diff Hunk:", "```diff", *diff_hunk.splitlines(), "```"])
    return lines


def _render_actor(value: object) -> str:
    if isinstance(value, dict):
        author = cast("dict[str, object]", value)
        login = str(author.get("login") or "unknown")
        name = author.get("name")
        if isinstance(name, str):
            normalized = name.strip()
            if normalized and normalized != login:
                return f"{login} ({normalized})"
        return login
    return "unknown"


def _render_reactions(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    icon_map = {
        "THUMBS_UP": "👍",
        "THUMBS_DOWN": "👎",
        "LAUGH": "😄",
        "HOORAY": "🎉",
        "CONFUSED": "😕",
        "HEART": "❤️",
        "ROCKET": "🚀",
        "EYES": "👀",
    }
    parts: list[str] = []
    reactions = cast("list[object]", value)
    for item in reactions:
        if not isinstance(item, dict):
            continue
        reaction = cast("dict[str, object]", item)
        users = reaction.get("users")
        if isinstance(users, dict):
            users_dict = cast("dict[str, object]", users)
            total = users_dict.get("totalCount")
        else:
            total = 0
        if not isinstance(total, int) or total <= 0:
            continue
        content = str(reaction.get("content") or "")
        emoji = icon_map.get(content, content.lower())
        parts.append(f"{emoji} x{total}")
    return ", ".join(parts) if parts else None


def _indent_block(text: str) -> list[str]:
    content_lines = text.splitlines() or [text]
    return [f"   {line}" for line in content_lines]


def _indented_tag_block(tag: str, content: str, indent: str = "") -> list[str]:
    out = [f"{indent}<{tag}>"]
    out.extend(f"{indent}{line}" for line in content.splitlines())
    out.append(f"{indent}</{tag}>")
    return out


def _extract_details_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for match in DETAILS_BLOCK_RE.finditer(text or ""):
        inner = (match.group(1) or "").strip()
        summary_match = SUMMARY_RE.search(inner)
        summary = "details"
        if summary_match is not None:
            summary = " ".join((summary_match.group(1) or "").split()) or "details"
            body = SUMMARY_RE.sub("", inner).strip()
        else:
            body = inner
        blocks.append((summary, body))
    return blocks
