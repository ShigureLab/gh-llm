import os
import stat
import subprocess
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_extension_entrypoint_forwards_to_uv_from_repo(tmp_path: Path) -> None:
    repo_root = _repo_root()
    script = repo_root / "gh-llm"
    fake_uv = tmp_path / "uv"
    captured = tmp_path / "captured.txt"
    fake_stdout = tmp_path / "out.txt"

    _write_executable(
        fake_uv,
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'echo "$@" > "$UV_FAKE_CAPTURE_FILE"',
                'echo "uv-called" > "$UV_FAKE_STDOUT_FILE"',
            ]
        )
        + "\n",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["UV_FAKE_CAPTURE_FILE"] = str(captured)
    env["UV_FAKE_STDOUT_FILE"] = str(fake_stdout)

    completed = subprocess.run(
        [str(script), "--help"],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == ""
    assert completed.stderr.strip() == ""
    assert fake_stdout.read_text(encoding="utf-8").strip() == "uv-called"
    forwarded = captured.read_text(encoding="utf-8").strip()
    assert " gh-llm --help" in forwarded
    assert "--no-group dev" in forwarded
    assert str(repo_root) in forwarded


def test_extension_entrypoint_requires_uv(tmp_path: Path) -> None:
    repo_root = _repo_root()
    script = repo_root / "gh-llm"

    completed = subprocess.run(
        ["/bin/bash", str(script), "--help"],
        cwd=repo_root,
        env={"PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == ""
    assert "uv is required" in completed.stderr
