import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_TARGET_HOST = "github.com"


def resolve_target_host() -> str:
    raw = os.environ.get("GH_HOST", "").strip()
    return raw or DEFAULT_TARGET_HOST


def build_auth_status_command(*, target_host: str | None = None) -> tuple[str, ...]:
    host = target_host or resolve_target_host()
    return ("gh", "auth", "status", "--active", "--hostname", host)


def render_command(cmd: Sequence[str]) -> str:
    return shlex.join(list(cmd))


def auth_status_command_text(*, target_host: str | None = None) -> str:
    return render_command(build_auth_status_command(target_host=target_host))
