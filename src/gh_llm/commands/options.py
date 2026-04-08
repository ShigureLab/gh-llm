from __future__ import annotations

import sys
from datetime import UTC, datetime
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from gh_llm.models import TimelineWindow

if TYPE_CHECKING:
    from collections.abc import Callable


def add_body_input_arguments(
    parser: Any,
    *,
    required: bool,
    body_help: str,
    file_help: str,
    default: str | None = None,
) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    body_kwargs: dict[str, object] = {"help": body_help}
    if default is not None:
        body_kwargs["default"] = default
    group.add_argument("--body", **body_kwargs)
    group.add_argument(
        "-F",
        "--body-file",
        help=file_help,
    )


def add_timeline_window_arguments(parser: Any) -> None:
    parser.add_argument(
        "--after",
        help="only include timeline events strictly after this ISO 8601 / RFC3339 timestamp",
    )
    parser.add_argument(
        "--before",
        help="only include timeline events strictly before this ISO 8601 / RFC3339 timestamp",
    )


def parse_timeline_window(*, after: str | None, before: str | None) -> TimelineWindow:
    after_value = _parse_timestamp(raw=after, flag="--after")
    before_value = _parse_timestamp(raw=before, flag="--before")
    if after_value is not None and before_value is not None and after_value >= before_value:
        raise RuntimeError("invalid time range: `--after` must be earlier than `--before`")
    return TimelineWindow(
        after=after_value,
        before=before_value,
        after_text=(format_timestamp_utc(after_value) if after_value is not None else None),
        before_text=(format_timestamp_utc(before_value) if before_value is not None else None),
    )


def format_timestamp_utc(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def current_timestamp_utc() -> str:
    return format_timestamp_utc(datetime.now(UTC).replace(microsecond=0))


def read_text_from_path_or_stdin(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def resolve_file_or_inline_text(
    args: Any,
    *,
    text_attr: str,
    file_attr: str,
    default: str = "",
) -> str:
    file_path = getattr(args, file_attr, None)
    if file_path is not None:
        return read_text_from_path_or_stdin(str(file_path))
    text = getattr(args, text_attr, None)
    if text is None:
        return default
    return str(text)


def resolve_subject[T](
    *,
    selector: str | None,
    repo: str | None,
    selector_flag: str,
    resolver: Callable[[str | None, str | None], T],
) -> T:
    _validate_selector_for_repo(selector=selector, repo=repo, selector_flag=selector_flag)
    return resolver(selector, repo)


def maybe_resolve_subject[T](
    *,
    selector: str | None,
    repo: str | None,
    selector_flag: str,
    resolver: Callable[[str | None, str | None], T],
) -> T | None:
    _validate_selector_for_repo(selector=selector, repo=repo, selector_flag=selector_flag)
    if selector is None:
        return None
    return resolver(selector, repo)


def _validate_selector_for_repo(*, selector: str | None, repo: str | None, selector_flag: str) -> None:
    if repo is not None and selector is None:
        raise RuntimeError(f"`{selector_flag}` is required when `--repo` is provided")


def raise_unknown_option_value(
    *,
    flag: str,
    token: str,
    valid_values: list[str],
    alias_values: list[str] | None = None,
) -> NoReturn:
    candidates = list(valid_values)
    if alias_values:
        candidates.extend(alias_values)
    suggestion = get_close_matches(token, candidates, n=1, cutoff=0.6)
    suggest_text = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
    valid_text = ", ".join(valid_values)
    raise RuntimeError(f"unknown {flag} option: {token}. Valid values: {valid_text}.{suggest_text}")


def _parse_timestamp(*, raw: str | None, flag: str) -> datetime | None:
    if raw is None:
        return None
    normalized = raw.strip()
    if not normalized:
        raise RuntimeError(f"{flag} requires a timestamp value")
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RuntimeError(f"invalid {flag} timestamp: {raw}") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(f"invalid {flag} timestamp: {raw}")
    return value.astimezone(UTC)
