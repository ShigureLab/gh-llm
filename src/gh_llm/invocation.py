import os
from pathlib import Path

DEFAULT_DISPLAY_COMMAND = "gh-llm"
DISPLAY_COMMAND_ENV = "GH_LLM_DISPLAY_CMD"


def display_command() -> str:
    raw = os.environ.get(DISPLAY_COMMAND_ENV, "").strip()
    if raw:
        return raw
    return DEFAULT_DISPLAY_COMMAND


def display_command_with(args: str) -> str:
    suffix = args.strip()
    if not suffix:
        return display_command()
    return f"{display_command()} {suffix}"


def detect_prog_name(argv0: str) -> str:
    name = Path(argv0).name
    if name:
        return name
    return DEFAULT_DISPLAY_COMMAND
