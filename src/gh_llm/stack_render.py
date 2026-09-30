from typing import TYPE_CHECKING

from gh_llm.invocation import display_command_with

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gh_llm.models import CheckItem, TimelineContext


def render_stack_section(context: TimelineContext, *, show_absent: bool = False) -> list[str]:
    stack = context.stack
    if not context.stack_supported:
        return ["## Stack", "Native stack metadata is unavailable on this GitHub server."]
    if stack is None:
        return ["## Stack", "(not in a native stack)"] if show_absent else []
    repo = f"{context.owner}/{context.name}"
    lines = [
        "## Stack",
        f"Stack #{stack.number}: layer {stack.position}/{stack.size}; target: `{stack.base_ref_name}`",
        "Order: bottom → top. CI is each PR's head rollup, including optional checks.",
    ]
    for entry in stack.entries:
        marker = " ← current" if entry.number == context.number else ""
        state = "DRAFT" if entry.is_draft and entry.state == "OPEN" else entry.state
        details = ", ".join(
            [
                f"merge: {entry.merge_state_status or 'UNKNOWN'}",
                f"review: {entry.review_decision or 'NONE'}",
                f"CI: {entry.checks_state or 'NONE'}",
            ]
        )
        lines.append(f"{entry.position}. #{entry.number} [{state}] {entry.title}{marker}")
        lines.append(f"   {details}; head: {entry.head_ref_oid or 'UNKNOWN'}")
    if not stack.complete:
        lines.append("Stack entries are incomplete; merge readiness is unknown.")
    lines.append(
        f"⏎ inspect a layer: `{display_command_with(f'pr view <pr_number> --repo {repo} --show meta,checks,mergeability')}`"
    )
    lines.append(f"⏎ layer diff: `{display_command_with(f'pr review-start --pr <pr_number> --repo {repo}')}`")
    return lines


def render_stack_mergeability(
    context: TimelineContext, checks: Sequence[CheckItem], *, requirements_known: bool | None = None
) -> list[str]:
    stack = context.stack
    assert stack is not None
    repo = f"{context.owner}/{context.name}"
    lines = ["## Mergeability"]
    if context.is_merged or context.state == "MERGED":
        return [*lines, "Status: Already merged", ""]
    if context.state == "CLOSED":
        return [*lines, "Status: Closed without merging", ""]
    scope = [entry for entry in stack.entries if entry.position <= stack.position and entry.state != "MERGED"]
    lines.append(
        f"Stack merge scope into `{stack.base_ref_name}`: "
        + (" → ".join(f"#{entry.number}" for entry in scope) or "unknown")
    )
    lines.append("Includes unmerged layers from the bottom through this PR; upper layers are excluded.")
    blockers: list[str] = []
    unknown = (
        not stack.complete or not any(entry.number == context.number for entry in scope) or requirements_known is False
    )
    for entry in scope:
        reasons: list[str] = []
        if entry.is_draft:
            reasons.append("draft")
        if entry.state != "OPEN":
            reasons.append(f"state: {entry.state}")
        if entry.mergeable == "CONFLICTING" or entry.merge_state_status == "DIRTY":
            reasons.append("merge conflicts")
        if entry.review_decision in {"REVIEW_REQUIRED", "CHANGES_REQUESTED"}:
            reasons.append(f"review: {entry.review_decision}")
        if entry.merge_state_status in {"BLOCKED", "BEHIND", "DRAFT"}:
            reasons.append(f"merge: {entry.merge_state_status}")
        if entry.merge_state_status in {None, "UNKNOWN"} or entry.mergeable in {None, "UNKNOWN"}:
            unknown = True
        if reasons:
            blockers.append(f"#{entry.number}: {', '.join(reasons)}")
    pending = [item.name for item in checks if item.required is True and not item.passed]
    if pending:
        blockers.append(f"#{context.number}: required checks not passed: {', '.join(pending)}")
    if blockers:
        lines.append("Status: Stack merge is blocked")
        lines.extend(f"- {reason}" for reason in blockers)
    elif unknown:
        lines.append("Status: Stack merge readiness is unknown")
    else:
        lines.append("Status: No blockers reported for this merge scope; confirm readiness on GitHub.")
    if not stack.complete:
        lines.append("Some stack entries are unavailable.")
    if requirements_known is False:
        lines.append(
            "Required check configuration is partially unavailable; merge readiness cannot be fully evaluated."
        )
    lines.append(
        "GitHub evaluates stack rules against the target branch. Optional CI failures alone are not merge blockers."
    )
    lines.append(f"⏎ open native stack merge controls: `gh pr view {context.number} --repo {repo} --web`")
    lines.append("")
    return lines
