# py-start

Project description goes here.

## Documentation Structure

This repository contains several documentation files for different audiences:

- **README.md** - User-facing project documentation for developers using or deploying this project
- [**AGENTS.md**](AGENTS.md) - Comprehensive development guidelines for AI agents,
  including code style, conventions, and workflows
  (Note: CLAUDE.md is symlinked to AGENTS.md in nix dev shell)

## Development

Update dependencies
```
nix flake update
```

Start nix dev shell
```
nix develop
```

NOTE: If you rename scripts in pyproject.toml, you may need to delete and recreate .venv
