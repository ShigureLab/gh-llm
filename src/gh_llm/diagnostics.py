from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gh_llm.environment import auth_status_command_text
from gh_llm.invocation import display_command_with
from gh_llm.transport_errors import looks_like_transport_error

if TYPE_CHECKING:
    from collections.abc import Sequence

_GRAPHQL_PROBE_COMMAND = "gh api graphql -f query='query{viewer{login}}'"
_REST_PROBE_COMMAND = "gh api user"
_RATE_LIMIT_COMMAND = "gh api rate_limit"
_GH_AUTH_LOGIN_COMMAND = "gh auth login"
_GRAPHQL_BACKED_COMMANDS = {
    ("gh", "api", "graphql"),
    ("gh", "pr", "view"),
    ("gh", "issue", "view"),
}


class GhCommandError(RuntimeError):
    def __init__(
        self,
        *,
        cmd: Sequence[str],
        stderr: str,
        stdout: str = "",
        attempts: int = 1,
        max_attempts: int = 1,
    ) -> None:
        self.cmd = tuple(str(part) for part in cmd)
        self.stderr = stderr.strip()
        self.stdout = stdout.strip()
        self.attempts = attempts
        self.max_attempts = max_attempts
        message = self.stderr or self.stdout or f"command failed: {shlex.join(list(self.cmd))}"
        super().__init__(message)

    @property
    def command_summary(self) -> str:
        if self.cmd[:3] == ("gh", "api", "graphql"):
            return "gh api graphql"
        if self.cmd[:3] == ("gh", "api", "user"):
            return "gh api user"
        if self.cmd[:3] == ("gh", "auth", "status"):
            return "gh auth status"
        if self.cmd[:3] == ("gh", "pr", "view"):
            return "gh pr view"
        if self.cmd[:3] == ("gh", "issue", "view"):
            return "gh issue view"
        return shlex.join(list(self.cmd))


@dataclass(frozen=True)
class _Diagnosis:
    headline: str
    category: str
    explanation: str
    next_commands: tuple[str, ...]


def format_command_error(error: GhCommandError) -> list[str]:
    diagnosis = _diagnose_command_error(error)
    lines = [f"error: {diagnosis.headline}"]

    detail = error.stderr or error.stdout
    if detail:
        lines.append(f"Last error: {detail}")
    lines.append(f"Category: {diagnosis.category}")
    lines.append(f"Command: {error.command_summary}")
    if diagnosis.explanation:
        lines.append(diagnosis.explanation)
    if diagnosis.next_commands:
        lines.append("")
        lines.append("Try next:")
        for command in diagnosis.next_commands:
            lines.append(f"- {command}")

    return lines


def _diagnose_command_error(error: GhCommandError) -> _Diagnosis:
    lowered = str(error).lower()
    if _looks_like_rate_limit_error(lowered):
        return _Diagnosis(
            headline="GitHub API request hit rate limiting.",
            category="rate limit",
            explanation="GitHub accepted the request but refused to serve it until the limit window resets.",
            next_commands=(_RATE_LIMIT_COMMAND, _auth_status_command(), display_command_with("doctor")),
        )

    if _looks_like_auth_error(lowered):
        return _Diagnosis(
            headline="GitHub authentication failed.",
            category="authentication",
            explanation="The current `gh` login or token does not look usable for this request.",
            next_commands=(
                _auth_status_command(),
                _GH_AUTH_LOGIN_COMMAND,
                _REST_PROBE_COMMAND,
                display_command_with("doctor"),
            ),
        )

    if _is_graphql_backed_command(error.cmd) and looks_like_transport_error(lowered):
        attempt_suffix = _format_attempt_suffix(error)
        return _Diagnosis(
            headline=f"GitHub GraphQL request failed{attempt_suffix}.",
            category="GraphQL transport / network",
            explanation=(
                "The request appears to have failed while GitHub GraphQL data was being fetched. "
                "This usually points to transient network, proxy, TLS, or GitHub-side transport issues. "
                "The direct REST and GraphQL probes are the useful source of truth here."
            ),
            next_commands=(
                _REST_PROBE_COMMAND,
                _GRAPHQL_PROBE_COMMAND,
                display_command_with("doctor"),
            ),
        )

    if _looks_like_subject_resolution_error(lowered, error):
        return _Diagnosis(
            headline="Could not resolve the requested repository / pull request / issue.",
            category="repository or selector resolution",
            explanation="The selector or repo looks invalid for the current command, or the resource is not visible.",
            next_commands=_resolution_commands(error.cmd),
        )

    return _Diagnosis(
        headline="GitHub CLI command failed.",
        category="generic gh failure",
        explanation="The underlying `gh` command did not complete successfully.",
        next_commands=(_auth_status_command(), display_command_with("doctor")),
    )


def _format_attempt_suffix(error: GhCommandError) -> str:
    if error.attempts <= 1:
        return ""
    return f" after {error.attempts} attempts"


def _auth_status_command() -> str:
    return auth_status_command_text()


def _is_graphql_backed_command(cmd: Sequence[str]) -> bool:
    return tuple(str(part) for part in cmd[:3]) in _GRAPHQL_BACKED_COMMANDS


def _looks_like_auth_error(lowered: str) -> bool:
    patterns = (
        "authentication failed",
        "bad credentials",
        "http 401",
        "401 unauthorized",
        "requires authentication",
        "gh auth login",
        "token is expired",
        "resource not accessible by integration",
        "not logged into any github hosts",
    )
    return any(pattern in lowered for pattern in patterns)


def _looks_like_rate_limit_error(lowered: str) -> bool:
    patterns = (
        "rate limit",
        "secondary rate limit",
        "api rate limit exceeded",
        "abuse detection",
    )
    return any(pattern in lowered for pattern in patterns)


def _looks_like_subject_resolution_error(lowered: str, error: GhCommandError) -> bool:
    if error.cmd[:3] in {
        ("gh", "pr", "view"),
        ("gh", "issue", "view"),
    }:
        patterns = (
            "could not resolve to a pullrequest",
            "could not resolve to an issue",
            "no pull requests found",
            "not found",
            "http 404",
        )
        return any(pattern in lowered for pattern in patterns)

    if error.cmd[:2] == ("gh", "api"):
        return "http 404" in lowered or "not found" in lowered

    return False


def _resolution_commands(cmd: Sequence[str]) -> tuple[str, ...]:
    command = tuple(str(part) for part in cmd)
    repo = _extract_option_value(command, "--repo") or "OWNER/REPO"
    if command[:3] == ("gh", "pr", "view"):
        selector = _extract_positional(command, prefix=("gh", "pr", "view")) or "<pr>"
        return (
            f"gh repo view {repo}",
            f"gh pr view {selector} --repo {repo}",
            display_command_with("doctor"),
        )
    if command[:3] == ("gh", "issue", "view"):
        selector = _extract_positional(command, prefix=("gh", "issue", "view")) or "<issue>"
        return (
            f"gh repo view {repo}",
            f"gh issue view {selector} --repo {repo}",
            display_command_with("doctor"),
        )
    return (_auth_status_command(), display_command_with("doctor"))


def _extract_option_value(cmd: Sequence[str], option: str) -> str | None:
    for index, token in enumerate(cmd):
        if token == option and index + 1 < len(cmd):
            return str(cmd[index + 1])
    return None


def _extract_positional(cmd: Sequence[str], *, prefix: tuple[str, ...]) -> str | None:
    start = len(prefix)
    if len(cmd) <= start:
        return None
    candidate = str(cmd[start])
    if candidate.startswith("-"):
        return None
    return candidate
