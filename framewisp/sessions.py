"""Resolve CLI session names without changing explicit path arguments."""

import hashlib
import os
import subprocess
from pathlib import Path


def project_directory() -> Path:
    directory = Path.cwd().resolve()
    # Git's repository overrides must not select a different project than cwd.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}
    }
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        raise ValueError(
            "Git is required to resolve session names. Install git or use an explicit session path."
        ) from None
    if result.returncode == 0:
        return Path(result.stdout.removesuffix("\n")).resolve()
    return directory


def resolve_session(argument: str) -> Path:
    if not argument or "\0" in argument or argument in {".", ".."}:
        raise ValueError(
            "Session name must be nonempty and cannot be '.' or '..'. "
            "Use an explicit path such as ./browser for a local directory."
        )
    if "/" in argument:
        return Path(argument).resolve()
    if len(os.fsencode(argument)) > 255:
        raise ValueError("Session name must fit in 255 bytes.")
    project = project_directory()
    digest = hashlib.sha256(os.fsencode(project)).hexdigest()[:24]
    return (Path("/tmp") / f"framewisp-project-{digest}" / argument).resolve()
