from typing import TYPE_CHECKING

from gh_llm.invocation import display_command_with

if TYPE_CHECKING:
    from gh_llm.models import TimelineContext


def render_merge_actions(context: TimelineContext) -> list[str]:
    if not context.head_ref_oid:
        return ["Merge actions unavailable: refresh the PR to load its head SHA."]
    available = [
        ("merge", context.merge_commit_allowed),
        ("squash", context.squash_merge_allowed),
        ("rebase", context.rebase_merge_allowed),
    ]
    enabled = [method for method, allowed in available if allowed is True]
    disabled = [method for method, allowed in available if allowed is False]
    lines: list[str] = []
    if enabled:
        lines.append("Merge actions (async; honors the target branch's merge queue):")
        subject = f"{context.title} (#{context.number})"
        if not context.stack:
            lines.append(f"⌨ merge_subject: '{subject}'")
            if context.co_author_trailers:
                lines.extend(["⌨ merge_body (default):", "   <optional_merge_body>", ""])
                lines.extend(f"   {trailer}" for trailer in context.co_author_trailers)
            else:
                lines.append("⌨ merge_body: '<optional_merge_body>'")
        for method in enabled:
            args = f"pr merge {context.number} --repo {context.owner}/{context.name} --{method} --head {context.head_ref_oid}"
            if method != "rebase" and not context.stack:
                quoted_subject = "'" + subject.replace("'", "'\"'\"'") + "'"
                args += f" --subject {quoted_subject} --body '<merge_body>'"
            lines.append(f"⏎ {method}: `{display_command_with(args)}`")
    if disabled:
        lines.append(f"Disabled by repository settings: {', '.join(disabled)}")
    return lines
