import argparse
import shlex
import sys

from gh_llm import __version__
from gh_llm.commands.doctor import register_doctor_parser
from gh_llm.commands.issue import register_issue_parser
from gh_llm.commands.pr import (
    parse_event_indexes as _parse_event_indexes,
    parse_review_ids as _parse_review_ids,
    register_pr_parser,
)
from gh_llm.commands.repo import register_repo_parser
from gh_llm.diagnostics import GhCommandError, format_command_error
from gh_llm.invocation import detect_prog_name


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return int(handler(args))
    except GhCommandError as error:
        for line in format_command_error(error):
            print(line, file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # pragma: no cover - covered by explicit CLI test
        command = shlex.join([detect_prog_name(sys.argv[0]), *argv])
        print(f"unexpected error: {error}", file=sys.stderr)
        print(file=sys.stderr)
        print("This is an unexpected gh-llm failure. Please consider reporting it.", file=sys.stderr)
        print(f"Command: {command}", file=sys.stderr)
        print("⌨ issue_title: '<short summary>'", file=sys.stderr)
        print("⌨ issue_body: '<what happened, expected result, actual result>'", file=sys.stderr)
        print(
            "⏎ Report via gh: `gh issue create --repo ShigureLab/gh-llm --title '<short summary>' --body '<what happened, expected result, actual result>'`",
            file=sys.stderr,
        )
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=detect_prog_name(sys.argv[0]),
        description="LLM-optimized GitHub reading, review, and interaction toolkit",
    )
    parser.add_argument("-v", "--version", action="version", version=__version__)

    subparsers = parser.add_subparsers(dest="command")
    register_pr_parser(subparsers)
    register_issue_parser(subparsers)
    register_repo_parser(subparsers)
    register_doctor_parser(subparsers)

    return parser


def parse_event_indexes(raw_indexes: list[str]) -> list[int]:
    return _parse_event_indexes(raw_indexes)


def parse_review_ids(raw_review_ids: list[str]) -> list[str]:
    return _parse_review_ids(raw_review_ids)
