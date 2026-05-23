from typing import TYPE_CHECKING, Any

from gh_llm.github_api import GitHubClient
from gh_llm.invocation import display_command_with

if TYPE_CHECKING:
    from gh_llm.models import RepoDocument, RepoPreflight


def register_repo_parser(subparsers: Any) -> None:
    repo_parser = subparsers.add_parser("repo", help="repository-level onboarding and preflight checks")
    repo_subparsers = repo_parser.add_subparsers(dest="repo_command")

    preflight_parser = repo_subparsers.add_parser(
        "preflight",
        help="show repository onboarding summary (docs, protection, permissions)",
    )
    preflight_parser.add_argument("--repo", required=True, help="repository in OWNER/REPO format")
    preflight_parser.set_defaults(handler=cmd_repo_preflight)


def cmd_repo_preflight(args: Any) -> int:
    client = GitHubClient()
    preflight = client.resolve_repo_preflight(str(args.repo))
    for line in render_repo_preflight(preflight):
        print(line)
    return 0


def render_repo_preflight(preflight: RepoPreflight) -> list[str]:
    return [
        *render_repo_frontmatter(preflight),
        "",
        *render_repo_summary(preflight),
        *render_repo_onboarding_files(preflight),
        *render_repo_branch_protection(preflight),
        *render_repo_next_commands(preflight),
    ]


def render_repo_frontmatter(preflight: RepoPreflight) -> list[str]:
    lines = [
        "---",
        f"repo: {preflight.owner}/{preflight.name}",
        f"url: {preflight.url}",
        f"default_branch: {preflight.default_branch}",
        f"viewer_permission: {preflight.viewer_permission or 'UNKNOWN'}",
        f"can_push: {str(preflight.can_push).lower()}",
        f"fork_recommended: {str(preflight.fork_recommended).lower()}",
        f"is_fork: {str(preflight.is_fork).lower()}",
        f"tree_truncated: {str(preflight.tree_truncated).lower()}",
    ]
    if preflight.parent_repo:
        lines.append(f"parent_repo: {preflight.parent_repo}")
    if preflight.branch_protection is not None:
        lines.append(f"branch_protection_pattern: {preflight.branch_protection.pattern}")
    lines.append("---")
    return lines


def render_repo_summary(preflight: RepoPreflight) -> list[str]:
    permission = preflight.viewer_permission or "UNKNOWN"
    https_clone_url = (
        preflight.url + ".git" if preflight.url else f"https://github.com/{preflight.owner}/{preflight.name}.git"
    )
    lines = [
        "## Repository",
        f"Description: {preflight.description or '(no description)'}",
        f"Default branch: `{preflight.default_branch}`",
        f"Homepage: {preflight.homepage_url or '(none)'}",
        f"HTTPS clone: `{https_clone_url}`",
        f"SSH clone: `{preflight.ssh_url or '(not available)'}`",
        f"Push access: {'yes' if preflight.can_push else 'no'} (viewer_permission: {permission})",
        f"Fork before push: {'yes' if preflight.fork_recommended else 'no'}",
    ]
    if preflight.is_fork and preflight.parent_repo:
        lines.append(f"Parent repo: `{preflight.parent_repo}`")
    if preflight.tree_truncated:
        lines.append(
            "Warning: recursive repository tree output was truncated; onboarding file detection used common-path fallback and may still be incomplete."
        )
    lines.append("")
    return lines


def render_repo_onboarding_files(preflight: RepoPreflight) -> list[str]:
    repo = f"{preflight.owner}/{preflight.name}"
    return [
        "## Onboarding Files",
        *render_repo_document_group(
            title="CONTRIBUTING",
            docs=preflight.contributing_docs,
            repo=repo,
            default_branch=preflight.default_branch,
        ),
        *render_repo_document_group(
            title="AGENTS",
            docs=preflight.agents_docs,
            repo=repo,
            default_branch=preflight.default_branch,
        ),
        *render_repo_document_group(
            title="PR Templates",
            docs=preflight.pr_templates,
            repo=repo,
            default_branch=preflight.default_branch,
        ),
        *render_repo_document_group(
            title="CODEOWNERS",
            docs=preflight.codeowners_files,
            repo=repo,
            default_branch=preflight.default_branch,
        ),
        "",
    ]


def render_repo_document_group(
    *,
    title: str,
    docs: tuple[RepoDocument, ...],
    repo: str,
    default_branch: str,
) -> list[str]:
    lines = [f"### {title}"]
    if not docs:
        lines.append("(not found)")
        return lines

    for doc in docs:
        browse_cmd = _gh_browse_command(repo=repo, default_branch=default_branch, path=doc.path)
        lines.append(f"- `{doc.path}`")
        lines.append(f"  ⏎ open: `{browse_cmd}`")
    return lines


def render_repo_branch_protection(preflight: RepoPreflight) -> list[str]:
    lines = ["## Branch Protection"]
    protection = preflight.branch_protection
    if protection is None:
        lines.append(f"Default branch `{preflight.default_branch}` is not protected.")
        lines.append("")
        return lines

    if protection.source == "graphql":
        lines.append(f"Matched rule: `{protection.pattern}`")
    else:
        lines.append(f"Protected branch: `{protection.pattern}`")
        lines.append("Review-related rule details were not available from branch rule queries.")
    if protection.requires_status_checks:
        if protection.required_status_check_contexts:
            lines.append(
                "Required checks: " + ", ".join(f"`{name}`" for name in protection.required_status_check_contexts)
            )
        else:
            lines.append("Required checks: enabled, but GitHub did not return named contexts.")
    else:
        lines.append("Required checks: not enabled")

    if protection.requires_approving_reviews is None:
        lines.append("Approving reviews: unknown")
    elif protection.requires_approving_reviews:
        required = protection.required_approving_review_count or 1
        lines.append(f"Approving reviews: required ({required})")
    else:
        lines.append("Approving reviews: not required")

    if protection.requires_code_owner_reviews is None:
        lines.append("Code owner reviews: unknown")
    else:
        lines.append(
            "Code owner reviews: " + ("required" if protection.requires_code_owner_reviews else "not required")
        )

    if protection.is_admin_enforced is None:
        lines.append("Admin enforcement: unknown")
    else:
        lines.append("Admin enforcement: " + ("enabled" if protection.is_admin_enforced else "disabled"))
    lines.append("")
    return lines


def render_repo_next_commands(preflight: RepoPreflight) -> list[str]:
    repo = f"{preflight.owner}/{preflight.name}"
    pr_target_repo = preflight.parent_repo if preflight.is_fork and preflight.parent_repo is not None else repo
    lines = [
        "## Next Commands",
        "These commands are inferred from the permission, onboarding-file, and default-branch protection signals above.",
    ]

    step = 1
    if preflight.fork_recommended:
        lines.append(f"{step}. Create your fork before pushing:")
        lines.append(f"   ⏎ `gh repo fork {repo} --clone`")
    else:
        lines.append(f"{step}. Clone the repository:")
        lines.append(f"   ⏎ `gh repo clone {repo}`")
    step += 1

    doc_steps = [
        ("Read contribution guidance", preflight.contributing_docs),
        ("Read AGENTS instructions", preflight.agents_docs),
        ("Inspect the PR template before opening a pull request", preflight.pr_templates),
        ("Check CODEOWNERS to see who may review your changes", preflight.codeowners_files),
    ]
    for title, docs in doc_steps:
        if not docs:
            continue
        lines.append(f"{step}. {title}:")
        lines.append(
            f"   ⏎ `{_gh_browse_command(repo=repo, default_branch=preflight.default_branch, path=docs[0].path)}`"
        )
        step += 1

    if pr_target_repo != repo:
        lines.append(f"{step}. Open your PR against the parent repository:")
    else:
        lines.append(f"{step}. Open your PR against the default branch:")
    lines.append(f"   ⏎ `gh pr create --repo {pr_target_repo} --base {preflight.default_branch}`")
    step += 1

    protection = preflight.branch_protection
    if protection is not None and protection.requires_status_checks:
        lines.append(f"{step}. Check required CI after the PR exists:")
        lines.append(f"   ⏎ `{display_command_with(f'pr checks --pr <pr_number> --repo {pr_target_repo}')}`")

    lines.append("")
    return lines


def _gh_browse_command(*, repo: str, default_branch: str, path: str) -> str:
    return f"gh browse -R {repo} --branch {default_branch} {_shell_single_quote(path)}"


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
