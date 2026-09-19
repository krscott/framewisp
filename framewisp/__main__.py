import argparse
import math
import time
from pathlib import Path

from framewisp.lib import (
    KEYS,
    drag_pointer,
    run_session,
    screenshot,
    send_input,
    type_text,
)


def seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError(
            "must be a finite, nonnegative number of seconds"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run and interact with a headless Wayland app."
    )
    parser.add_argument("--session", type=Path, required=True, help="session directory")
    commands = parser.add_subparsers(dest="action", required=True)

    run = commands.add_parser(
        "run", help="run an app until it exits or you interrupt it"
    )
    run.add_argument(
        "--record", type=Path, metavar="FILE", help="record the session to MP4"
    )
    run.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- executable [args...]"
    )

    capture = commands.add_parser(
        "screenshot", help="save the current display as a PNG"
    )
    capture.add_argument("path", type=Path)
    capture.add_argument(
        "--delay",
        type=seconds,
        default=0.0,
        metavar="SECONDS",
        help="wait this many seconds before capturing (default: 0)",
    )

    click = commands.add_parser(
        "click", help="send a left click at display coordinates"
    )
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)

    drag = commands.add_parser("drag", help="drag with the left mouse button")
    drag.add_argument("x1", type=int)
    drag.add_argument("y1", type=int)
    drag.add_argument("x2", type=int)
    drag.add_argument("y2", type=int)
    drag.add_argument(
        "--duration",
        type=seconds,
        default=0.4,
        metavar="SECONDS",
        help="time spent dragging (default: 0.4; 0 moves immediately)",
    )

    typing = commands.add_parser("type", help="type printable ASCII text")
    typing.add_argument("text")
    typing.add_argument(
        "--interval",
        type=seconds,
        default=0.08,
        metavar="SECONDS",
        help="time between characters (default: 0.08; 0 types immediately)",
    )

    key = commands.add_parser("key", help="press and release a named key")
    key.add_argument("name", choices=KEYS)

    args = parser.parse_args()
    session = args.session.resolve()
    if args.action == "run":
        command: list[str] = args.command
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            parser.error("run requires an application command after --")
        result = run_session(session, command, recording=args.record)
    elif args.action == "screenshot":
        if args.delay:
            time.sleep(args.delay)
        result = screenshot(session, args.path)
    elif args.action == "click":
        result = send_input(session, ["move", str(args.x), str(args.y), "click", "1"])
    elif args.action == "drag":
        result = drag_pointer(
            session, args.x1, args.y1, args.x2, args.y2, duration=args.duration
        )
    elif args.action == "type":
        if any(not 32 <= ord(char) <= 126 for char in args.text):
            parser.error("type supports printable ASCII only")
        result = type_text(session, args.text, interval=args.interval)
    else:
        result = send_input(session, ["key", KEYS[args.name]])
    raise SystemExit(result)


if __name__ == "__main__":
    main()
