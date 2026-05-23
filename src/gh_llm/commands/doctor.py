import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from gh_llm import __version__
from gh_llm.environment import build_auth_status_command, resolve_target_host
from gh_llm.invocation import detect_prog_name, display_command

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

_GRAPHQL_PROBE_QUERY = "query{viewer{login}}"
_ENV_KEYS = (
    "GH_LLM_DISPLAY_CMD",
    "GH_HOST",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


@dataclass(frozen=True)
class _CommandResult:
    ok: bool
    output: str


@dataclass(frozen=True)
class _ProbeResult:
    name: str
    command: str
    ok: bool
    summary: str
    detail: str = ""
    critical: bool = True


def register_doctor_parser(subparsers: Any) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="diagnose environment, authentication, and connectivity",
    )
    doctor_parser.set_defaults(handler=cmd_doctor)


def cmd_doctor(_: Any) -> int:
    entrypoint = display_command()
    argv0 = detect_prog_name(sys.argv[0])
    target_host = resolve_target_host()
    entrypoint_probe = _probe_entrypoint_version(entrypoint)
    gh_version_probe = _probe_gh_version()
    auth_status_probe = _probe_auth_status(target_host)
    rest_user_probe = _probe_rest_user()
    graphql_viewer_probe = _probe_graphql_viewer()
    auth_status_probe = _reconcile_auth_status_probe(
        auth_status_probe,
        rest_user_probe=rest_user_probe,
        graphql_viewer_probe=graphql_viewer_probe,
    )
    critical_probes = (
        entrypoint_probe,
        gh_version_probe,
        auth_status_probe,
        rest_user_probe,
        graphql_viewer_probe,
    )
    failed = [probe.name for probe in critical_probes if not probe.ok and probe.critical]

    lines: list[str] = [
        "## Entrypoint",
        f"- entrypoint: {entrypoint}",
        f"- argv0: {argv0}",
        f"- argv0_path: {_resolve_argv0_path(sys.argv[0], argv0)}",
        f"- entrypoint_path: {_resolve_entrypoint_path(entrypoint)}",
        f"- gh_path: {_resolve_binary_path('gh')}",
        f"- gh_llm_path: {_resolve_binary_path('gh-llm')}",
        f"- target_host: {target_host}",
        f"- python: {sys.executable}",
        f"- cwd: {Path.cwd()}",
        "",
        "## Versions",
        f"- package: {__version__}",
        f"- python: {sys.version.split()[0]}",
    ]
    lines.extend(_render_probe_lines(critical_probes[:2]))
    lines.extend(
        [
            "",
            "## Environment",
        ]
    )
    lines.extend(_render_environment_lines())
    lines.extend(
        [
            "",
            "## Auth",
        ]
    )
    lines.extend(_render_probe_lines((critical_probes[2],)))
    lines.extend(
        [
            "",
            "## Probes",
        ]
    )
    lines.extend(_render_probe_lines(critical_probes[3:]))
    lines.extend(
        [
            "",
            "## Summary",
            f"status: {'ok' if not failed else 'unhealthy'}",
        ]
    )
    if failed:
        lines.append(f"failed_checks: {', '.join(failed)}")

    for line in lines:
        print(line)
    return 0 if not failed else 1


def _render_probe_lines(probes: Sequence[_ProbeResult]) -> list[str]:
    lines: list[str] = []
    for probe in probes:
        lines.append(f"- {probe.name} (`{probe.command}`): {probe.summary}")
        detail = probe.detail.strip()
        if detail and detail != probe.summary:
            lines.extend(_indent_block(detail))
    return lines


def _render_environment_lines() -> list[str]:
    lines: list[str] = []
    for key in _ENV_KEYS:
        lines.append(f"- {key}: {_format_env_value(key, os.environ.get(key))}")
    return lines


def _probe_entrypoint_version(entrypoint: str) -> _ProbeResult:
    command = _split_command(entrypoint, suffix=("--version",))
    rendered = shlex.join(command)
    result = _run_command(command)
    return _probe_from_command_result(
        name="entrypoint version",
        command=rendered,
        result=result,
        success_summary=result.output.strip() or "ok",
    )


def _probe_gh_version() -> _ProbeResult:
    command = ["gh", "--version"]
    result = _run_command(command)
    return _probe_from_command_result(
        name="gh version",
        command=shlex.join(command),
        result=result,
        success_summary=(result.output.splitlines()[0].strip() if result.output.strip() else "ok"),
    )


def _probe_auth_status(target_host: str) -> _ProbeResult:
    command = list(build_auth_status_command(target_host=target_host))
    result = _run_command(command)
    summary = "ok" if result.ok else "failed"
    return _ProbeResult(
        name="auth status",
        command=shlex.join(command),
        ok=result.ok,
        summary=summary,
        detail=result.output,
    )


def _reconcile_auth_status_probe(
    auth_status_probe: _ProbeResult,
    *,
    rest_user_probe: _ProbeResult,
    graphql_viewer_probe: _ProbeResult,
) -> _ProbeResult:
    if auth_status_probe.ok or not (rest_user_probe.ok and graphql_viewer_probe.ok):
        return auth_status_probe

    detail_parts = [
        part
        for part in (auth_status_probe.detail.strip(), "API probes succeeded; treating auth status as a warning.")
        if part
    ]
    return _ProbeResult(
        name=auth_status_probe.name,
        command=auth_status_probe.command,
        ok=False,
        summary="warning (API probes ok)",
        detail="\n\n".join(detail_parts),
        critical=False,
    )


def _probe_rest_user() -> _ProbeResult:
    command = ["gh", "api", "user"]
    result = _run_command(command)
    if not result.ok:
        return _ProbeResult(
            name="REST user probe",
            command=shlex.join(command),
            ok=False,
            summary="failed",
            detail=result.output,
        )

    login = _extract_json_login(result.output, path=("login",))
    summary = f"ok (@{login})" if login else "ok"
    return _ProbeResult(
        name="REST user probe",
        command=shlex.join(command),
        ok=True,
        summary=summary,
        detail="",
    )


def _probe_graphql_viewer() -> _ProbeResult:
    command = ["gh", "api", "graphql", "-f", f"query={_GRAPHQL_PROBE_QUERY}"]
    result = _run_command(command)
    if not result.ok:
        return _ProbeResult(
            name="GraphQL viewer probe",
            command="gh api graphql -f query='query{viewer{login}}'",
            ok=False,
            summary="failed",
            detail=result.output,
        )

    login = _extract_json_login(result.output, path=("data", "viewer", "login"))
    summary = f"ok (@{login})" if login else "ok"
    return _ProbeResult(
        name="GraphQL viewer probe",
        command="gh api graphql -f query='query{viewer{login}}'",
        ok=True,
        summary=summary,
        detail="",
    )


def _probe_from_command_result(
    *,
    name: str,
    command: str,
    result: _CommandResult,
    success_summary: str,
) -> _ProbeResult:
    if result.ok:
        return _ProbeResult(name=name, command=command, ok=True, summary=success_summary, detail=result.output)
    return _ProbeResult(name=name, command=command, ok=False, summary="failed", detail=result.output)


def _run_command(cmd: Sequence[str]) -> _CommandResult:
    try:
        completed = subprocess.run(list(cmd), check=False, capture_output=True, text=True)
    except OSError as error:
        return _CommandResult(ok=False, output=str(error))
    return _CommandResult(ok=completed.returncode == 0, output=_merge_outputs(completed.stdout, completed.stderr))


def _merge_outputs(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part.strip()]
    return "\n\n".join(parts)


def _extract_json_login(payload: str, *, path: Sequence[str]) -> str | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    current: object = parsed
    for key in path:
        if not isinstance(current, dict):
            return None
        current_dict = cast("dict[str, object]", current)
        current = current_dict.get(key)
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def _indent_block(text: str) -> list[str]:
    return [f"  {line}" if line else "" for line in text.splitlines()]


def _split_command(command: str, *, suffix: Sequence[str] = ()) -> list[str]:
    base = shlex.split(command)
    return [*base, *suffix]


def _resolve_argv0_path(argv0_raw: str, argv0_name: str) -> str:
    if argv0_raw:
        candidate = Path(argv0_raw)
        if candidate.exists():
            return str(candidate.resolve())
    return _resolve_binary_path(argv0_name)


def _resolve_entrypoint_path(entrypoint: str) -> str:
    parts = shlex.split(entrypoint)
    if not parts:
        return "(unknown)"
    return _resolve_binary_path(parts[0])


def _resolve_binary_path(name: str) -> str:
    resolved = shutil.which(name)
    return resolved or "(not found)"


def _format_env_value(key: str, value: str | None) -> str:
    if value is None or not value.strip():
        return "(unset)"
    if key in {"GH_TOKEN", "GITHUB_TOKEN"}:
        return "(set)"
    return _redact_url_credentials(value.strip())


def _redact_url_credentials(value: str) -> str:
    if "://" not in value or "@" not in value:
        return value
    parsed = urlsplit(value)
    if not parsed.hostname:
        return value
    netloc = parsed.hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username is not None:
        netloc = f"***@{netloc}"
    redacted = SplitResult(
        scheme=parsed.scheme,
        netloc=netloc,
        path=parsed.path,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunsplit(redacted)
