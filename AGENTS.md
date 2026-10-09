# Agent guide for framewisp

framewisp is a tool that gives agents their own display to control GUI apps.

It is early dev and has no users yet. Improving software quality is more
important than maintaining backwards compatibility.

Code from the original py-start template may be removed as the project evolves.

## Development environment

Use `nix develop` to enter the development shell. Its hooks create `.venv` if
missing, activate it, and install the project with development dependencies on
creation. Agents can run this themselves; no user restart is needed.

For noninteractive commands, use `nix develop -c <command>`, for example:

```sh
nix develop -c python -m pytest
```

Run Python tools such as pytest, mypy, and pyright with `python -m <tool>` so
they use the active environment and can find its installed packages.

## Dependencies

Declare dependencies before installing them:

1. Add runtime dependencies to `pyproject.toml` under `dependencies`, or
   development dependencies under `project.optional-dependencies.dev`.
2. Add corresponding Nix runtime packages to `default.nix`. Add development
   tools needed by the Nix environment to `flake.nix` and test dependencies
   needed during package checks to `default.nix` under `nativeCheckInputs`.
3. Run `python -m pip install -e '.[dev]'` in the development shell to refresh
   the installation.

For libraries without type hints, add available stub packages to development
dependencies. If stubs are unavailable, use a mypy override scoped to the
affected module in `pyproject.toml`.

## Validation

| Check | Command |
|-------|---------|
| Pyright | `python -m pyright` |
| Mypy | `python -m mypy .` |
| Tests | `python -m pytest` |

Both type checkers must pass. Keep any necessary type-checking exceptions narrow
and explain why they are needed.

Use pytest fixtures for setup and teardown. Minimize mocks.

## Code style

- Use Python 3.10+ type hints, such as `list[str | None]`.
- Keep imports at the top of the file unless a local import is necessary.
- Prefer returning values over raising exceptions. Make failures actionable.
- Use `pathlib.Path` instead of `os.path`.
- Prefer composition over inheritance and keep data easy to inspect, test,
  and serialize.

## Design documentation

Keep `DESIGN.md` current with application requirements and architecture. It
should contain enough detail for an agent to reproduce the application.
