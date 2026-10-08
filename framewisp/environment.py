"""Keep the caller's app environment separate from framewisp's runtime."""

import os

_caller_environment: dict[str, str] | None = None


def initialize_environment() -> None:
    """Consume and close the launcher descriptor before starting any children."""
    global _caller_environment
    descriptor = os.environ.pop("FRAMEWISP_CALLER_ENV_FD", None)
    if descriptor is None:
        return
    with os.fdopen(int(descriptor), "rb") as snapshot:
        entries = snapshot.read().split(b"\0")
    _caller_environment = {}
    for entry in entries:
        if entry:
            key, value = entry.split(b"=", 1)
            _caller_environment[os.fsdecode(key)] = os.fsdecode(value)
    _caller_environment.pop("FRAMEWISP_CALLER_ENV_FD", None)


def caller_environment() -> dict[str, str]:
    """Editable installs have no wrapper, so their current environment is the caller's."""
    return dict(os.environ if _caller_environment is None else _caller_environment)
