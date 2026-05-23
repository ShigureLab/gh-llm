from __future__ import annotations

import argparse

import pytest

from gh_llm.commands import options as command_options


def test_add_body_input_arguments_preserves_optional_default() -> None:
    parser = argparse.ArgumentParser()
    command_options.add_body_input_arguments(
        parser,
        required=False,
        body_help="review body",
        file_help="read review body from file",
        default="Suggested change",
    )

    args = parser.parse_args([])

    assert args.body == "Suggested change"
    assert args.body_file is None


def test_resolve_subject_requires_selector_when_repo_is_provided() -> None:
    with pytest.raises(RuntimeError, match=r"`--pr` is required when `--repo` is provided"):
        command_options.resolve_subject(
            selector=None,
            repo="PaddlePaddle/Paddle",
            selector_flag="--pr",
            resolver=lambda selector, repo: None,
        )


def test_maybe_resolve_subject_skips_resolution_without_selector() -> None:
    called = False

    def fake_resolver(selector: str | None, repo: str | None) -> str:
        nonlocal called
        called = True
        return f"{selector}@{repo}"

    resolved = command_options.maybe_resolve_subject(
        selector=None,
        repo=None,
        selector_flag="--issue",
        resolver=fake_resolver,
    )

    assert resolved is None
    assert called is False


def test_parse_auto_collapse_authors_accepts_commas_repeats_and_at_prefixes() -> None:
    resolved = command_options.parse_auto_collapse_authors(
        raw_values=["PaddlePaddle-bot,@other", "@paddlepaddle-BOT", "  third  "]
    )

    assert resolved == ("PaddlePaddle-bot", "other", "third")


def test_resolve_file_or_inline_text_treats_empty_file_path_as_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_read_text_from_path_or_stdin(path: str) -> str:
        seen.append(path)
        return "from file"

    monkeypatch.setattr(command_options, "read_text_from_path_or_stdin", fake_read_text_from_path_or_stdin)
    args = argparse.Namespace(body="inline body", body_file="")

    resolved = command_options.resolve_file_or_inline_text(args, text_attr="body", file_attr="body_file")

    assert resolved == "from file"
    assert seen == [""]
