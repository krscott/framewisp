"""Handle standalone commands before loading desktop and capture libraries."""

import sys
from pathlib import Path

from framewisp.desktop import detach_desktop


def main() -> None:
    if sys.argv[1:] == ["--agent-skill"]:
        sys.stdout.write(
            Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8")
        )
        return
    if sys.argv[1:] == ["--detach"]:
        raise SystemExit(detach_desktop())
    # Delay optional desktop imports until after the emergency-stop path.
    from framewisp.cli import main as command_main

    command_main()


if __name__ == "__main__":
    main()
