import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from framewisp.connection import COMMAND_PROTOCOL
from framewisp.sessions import resolve_session_dir


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        timeout=5,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    git(directory, "init")
    git(
        directory,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "Initial commit",
    )
    return directory


def test_outside_git_scopes_to_canonical_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    digest = hashlib.sha256(os.fsencode(tmp_path.resolve())).hexdigest()[:24]
    expected = Path(tempfile.gettempdir()) / f"framewisp-project-{digest}" / "browser"
    assert resolve_session_dir("browser") == expected
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert resolve_session_dir("browser") != expected
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.chdir(alias)
    assert resolve_session_dir("browser") == expected
    assert not expected.exists()


# R-SESSION-1, R-SESSION-2
def test_git_subdirectories_symlinks_and_worktrees(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repository)
    expected = resolve_session_dir("browser")
    child = repository / "nested" / "child"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert resolve_session_dir("browser") == expected
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    monkeypatch.chdir(alias / "nested" / "child")
    assert resolve_session_dir("browser") == expected
    other = tmp_path / "other-worktree"
    git(repository, "worktree", "add", "-b", "other", str(other))
    monkeypatch.chdir(other)
    assert resolve_session_dir("browser") != expected
    other_child = other / "child"
    other_child.mkdir()
    monkeypatch.chdir(other_child)
    assert resolve_session_dir("browser") == resolve_in_process(other, "browser")
    assert resolve_in_process(child, "browser") == expected


def resolve_in_process(directory: Path, argument: str) -> Path:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(root)!r}); "
            "from framewisp.sessions import resolve_session_dir; "
            "print(resolve_session_dir(sys.argv[1]))",
            argument,
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return Path(result.stdout.strip())


def test_git_overrides_do_not_select_another_project(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repository)
    expected = resolve_session_dir("browser")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_COMMON_DIR", str(other / ".git"))
    assert resolve_session_dir("browser") == expected


def test_git_errors_do_not_change_project_scope(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = repository / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    (repository / ".git" / "config").write_text("[unterminated")
    with pytest.raises(ValueError, match="Fix Git configuration"):
        resolve_session_dir("browser")


@pytest.mark.parametrize("name", [b"project-\xff", b"root\rname", b"root\nname"])
def test_git_root_preserves_filesystem_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: bytes
) -> None:
    directory = tmp_path / os.fsdecode(name)
    directory.mkdir()
    git(directory, "init")
    monkeypatch.chdir(directory)
    digest = hashlib.sha256(os.fsencode(directory.resolve())).hexdigest()[:24]
    assert resolve_session_dir("browser") == (
        Path(tempfile.gettempdir()) / f"framewisp-project-{digest}" / "browser"
    )


@pytest.mark.parametrize(
    "argument", ["./browser", "sessions/browser", "browser/", "./", "../"]
)
def test_explicit_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert resolve_session_dir(argument) == (tmp_path / argument).resolve()


def test_explicit_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    assert resolve_session_dir(str(tmp_path)) == tmp_path.resolve()


# R-SESSION-4
@pytest.mark.parametrize("argument", ["", ".", "..", "bad\0name", "a" * 256, "é" * 128])
def test_invalid_names(argument: str) -> None:
    with pytest.raises(ValueError):
        resolve_session_dir(argument)


def test_names_require_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(
        ValueError, match="Install git or give the session directory's path"
    ):
        resolve_session_dir("browser")


# R-SESSION-3
@pytest.mark.parametrize("attached", [False, True])
def test_cli_resolves_named_session_without_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attached: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    session_dir = resolve_session_dir("browser")
    session_dir.mkdir(parents=True)
    metadata = (
        {
            "kind": "attached",
            "command_protocol": COMMAND_PROTOCOL,
            "command_socket": str(session_dir / "command.sock"),
            "share_token": "token",
        }
        if attached
        else {}
    )
    (session_dir / "session.json").write_text(json.dumps(metadata))
    legacy = tmp_path / "browser"
    legacy.mkdir()
    (legacy / "session.json").write_text("invalid legacy metadata")
    try:
        for argument in ["browser", str(session_dir)]:
            result = subprocess.run(
                [sys.executable, "-m", "framewisp", argument, "status"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            assert result.returncode == 1
            assert str(session_dir) in result.stderr
            assert (
                "The user ends this session with Ctrl+C in its terminal."
                if attached
                else "another version of framewisp"
            ) in result.stderr
            assert "--stop-sharing" not in result.stderr
        (session_dir / "session.json").unlink()
        result = subprocess.run(
            [sys.executable, "-m", "framewisp", "browser", "status"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert "Session is disconnected" in result.stderr
    finally:
        (session_dir / "session.json").unlink(missing_ok=True)
        session_dir.rmdir()
        session_dir.parent.rmdir()
