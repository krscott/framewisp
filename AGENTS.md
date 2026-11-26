# Agent guide for framewisp

framewisp is a tool that gives agents their own display to control GUI apps.

It is early dev and has no users yet. Improving software quality is more
important than maintaining backwards compatibility.

Code from the original py-start template may be removed as the project evolves.

## Development environment

Use `nix develop` to enter the development shell. Nix supplies runtime and
development dependencies, including setuptools. Its hooks create `.venv` if
missing, activate it, and install only the local project in editable mode,
without resolving or downloading Python packages. Each worktree has its own
editable installation. Agents can run this themselves; no user restart is needed.

For noninteractive commands, use `nix develop -c <command>`, for example:

```sh
nix develop -c python -m pytest
```

Run Python tools such as pytest and mypy with `python -m <tool>` so
they use the active environment and can find its installed packages.

Run `pyright` directly. In the Nix shell, Nix supplies the Node program and the
creation hook skips pip dependencies. For manual setup, run
`python -m pip install -e '.[dev]'` to install the Python wrapper, which provides
the same command.
It may download Node and pyright on first use. Nix is optional; install native
runtime dependencies and Just separately when working without it.

## Dependencies

Declare dependencies before installing them:

1. Add runtime dependencies to `pyproject.toml` under `dependencies`, or
   development dependencies under `project.optional-dependencies.dev`.
2. Add corresponding Nix runtime packages to `default.nix`. Add development
   tools needed by the Nix environment to `flake.nix` and test dependencies
   needed during package checks to `default.nix` under `nativeCheckInputs`.
3. Exit and re-enter `nix develop` to load the updated Nix dependencies. To
   refresh package metadata or script entry points in an existing `.venv`, run
   `python -m pip install --no-deps --no-build-isolation -e .` in that shell.

For libraries without type hints, add available stub packages to development
dependencies. If stubs are unavailable, use a mypy override scoped to the
affected module in `pyproject.toml`.

## Validation

| Check | Command |
|-------|---------|
| Pyright | `pyright` |
| Mypy | `python -m mypy .` |
| Tests | `python -m pytest` |
| Installed-package tests | `nix flake check` |
| Format | `nix run .#format` (CI fails if this changes files) |

`tests/test_python_environment.py` runs only in the installed-package checks,
which split cases across twelve shards. Run `nix flake check` when changing how
framewisp or the app gets its environment.
The installed-package checks also run `test_integration.py`,
`test_app_environment.py` and `test_unresponsive_app.py` without the `framewisp`
module on the Python path, so those files must not import it.

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

## Requirements, glossary and decisions

- `REQUIREMENTS.md` lists the observable behaviours and invariants framewisp
  must keep, by stable ID.
- `GLOSSARY.md` defines the domain terms. It is the naming authority for code,
  the CLI, file formats and docs: use its term for each concept, and do not use
  a word listed under a term's _Avoid_ for that term's concept. When a concept
  has no term, or a term no longer fits, change the glossary in the same PR.
  Keep implementation detail out of it.
  Existing code and docs still use some avoided words; issues #196 to #200
  rename them. Use the glossary's terms in new work.
  TODO: remove these two sentences once those issues are closed and no avoided
  word is left in its avoided sense.
- `docs/adr/` records decisions that are hard to reverse, surprising without
  context, and the result of a real trade-off. Read the ADRs for an area before
  changing it, and add one only when all three hold.
- A reason that explains one piece of code belongs in a comment at that code.

## Change order

- Work flows from `REQUIREMENTS.md` to code and tests. Start at the highest layer the change affects; most PRs change only code.
- For a bug fix, check whether a requirement was missing or violated. Add one when it captures a valuable durable guarantee; an implementation defect does not automatically create a new product promise.
- Never renumber, reuse or delete a requirement ID; tag it `[retired]` instead. Tests covering a `[test]` requirement carry its ID in a comment.

## Command protocol

When a change lets a newer CLI ask for something that a session's older runner
would accept but not do, make the CLI refuse clearly: the runner advertises the
feature in session metadata (`session.json`), and the CLI checks that before it
relies on it. A session command that the older runner already rejects needs no
check.
