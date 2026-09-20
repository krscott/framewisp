import argparse
import json
import math
import subprocess
import time
from pathlib import Path

from framewisp.captions import log_input
from framewisp.connection import request_attached
from framewisp.desktop import detach_desktop
from framewisp.errors import SessionError
from framewisp.lib import (
    CLICK_BUTTONS,
    MODIFIERS,
    SCROLL_BUTTONS,
    click_pointer,
    drag_pointer,
    key_commands,
    run_session,
    screenshot,
    scroll_pointer,
    send_input,
    session_command,
    type_text,
)


def seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError(
            "must be a finite, nonnegative number of seconds"
        )
    return result


def positive_integer(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a headless Wayland or X11 app, or attach to your desktop."
    )
    parser.add_argument(
        "--agent-skill",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the bundled agent skill Markdown and exit (use alone)",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="stop the active desktop attachment, regardless of session",
    )
    parser.add_argument(
        "session", nargs="?", type=Path, metavar="SESSION", help="session directory"
    )
    commands = parser.add_subparsers(dest="action")

    commands.add_parser(
        "status", help="report live headless app and recording state as JSON"
    )
    commands.add_parser("stop", help="stop a headless session and wait for cleanup")

    commands.add_parser(
        "attach", help="share an existing desktop through its permission dialog"
    )

    run = commands.add_parser(
        "run", help="run an app until it exits or you interrupt it"
    )
    run.add_argument(
        "--x11", action="store_true", help="run the app on a private Xwayland display"
    )
    run.add_argument(
        "--record", type=Path, metavar="FILE", help="record the session to MP4"
    )
    run.add_argument(
        "--width",
        type=positive_integer,
        default=1280,
        help="display width in pixels (default: 1280)",
    )
    run.add_argument(
        "--height",
        type=positive_integer,
        default=720,
        help="display height in pixels (default: 720)",
    )
    run.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- executable [args...]"
    )

    record_start = commands.add_parser("record-start", help="start recording a clip")
    record_start.add_argument("path", type=Path, metavar="FILE")
    for recording_parser in (run, record_start):
        recording_parser.add_argument(
            "--no-captions",
            action="store_true",
            help="record without input captions (input logging stays enabled)",
        )
    commands.add_parser(
        "record-stop", help="finalize the active clip and keep the app running"
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

    move = commands.add_parser("move", help="move the pointer without pressing buttons")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)

    click = commands.add_parser(
        "click", help="send one or two clicks at display coordinates"
    )
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument(
        "--button",
        choices=CLICK_BUTTONS,
        default="left",
        help="mouse button (default: left)",
    )
    click.add_argument(
        "--count",
        type=int,
        choices=[1, 2],
        default=1,
        help="click count, with 0.1 seconds between clicks (default: 1)",
    )

    scroll = commands.add_parser(
        "scroll", help="send wheel steps at display coordinates"
    )
    scroll.add_argument("x", type=int)
    scroll.add_argument("y", type=int)
    scroll.add_argument("direction", choices=SCROLL_BUTTONS)
    scroll.add_argument(
        "--steps", type=int, default=1, help="positive wheel step count (default: 1)"
    )

    drag = commands.add_parser("drag", help="drag with a mouse button")
    drag.add_argument(
        "--button",
        choices=CLICK_BUTTONS,
        default="left",
        help="mouse button (default: left)",
    )
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

    for gesture in (click, drag):
        gesture.add_argument(
            "--modifier",
            action="append",
            choices=sorted(MODIFIERS),
            type=str.lower,
            default=[],
            help="hold a modifier for the gesture; repeat for combinations",
        )

    typing = commands.add_parser("type", help="type printable Unicode text")
    typing.add_argument("text")
    typing.add_argument(
        "--interval",
        type=seconds,
        default=0.08,
        metavar="SECONDS",
        help="time between characters (default: 0.08; 0 types immediately)",
    )

    key = commands.add_parser(
        "key",
        help="press and release a key or shortcut",
        description="Send a key with optional Ctrl, Shift, and Alt modifiers. "
        "Names are case-insensitive; letter case does not imply Shift. "
        "Keys: a-z, 0-9, Space, Return, Tab, BackSpace, Escape, Delete, Left, Right, Up, Down.",
    )
    key.add_argument(
        "chord", metavar="CHORD", help="for example: Return, Ctrl+a, Ctrl+Shift+z"
    )

    args = parser.parse_args()
    if getattr(args, "agent_skill", False):
        parser.error("--agent-skill must be used alone")
    if args.detach:
        if args.session is not None or args.action is not None:
            parser.error("--detach takes no session or command")
        raise SystemExit(detach_desktop())
    if args.session is None or args.action is None:
        parser.error(
            "SESSION and COMMAND are required (or use --detach or --agent-skill)"
        )
    session = args.session.resolve()
    if args.action in {"click", "drag"} and len(set(args.modifier)) != len(
        args.modifier
    ):
        parser.error("each --modifier may only be specified once")
    if args.action == "scroll" and args.steps < 1:
        parser.error("--steps must be a positive integer")
    if args.action == "type" and any(not char.isprintable() for char in args.text):
        parser.error(
            "type supports printable characters only; use key for Return or Tab"
        )
    if args.action == "key" and key_commands(args.chord) is None:
        parser.error(
            "unsupported key combination; use key --help for supported keys and modifiers"
        )
    if args.action == "run":
        if not args.command or args.command == ["--"]:
            parser.error("run requires an application command after --")
        if args.record is not None and (args.width % 2 or args.height % 2):
            parser.error("--record requires even --width and --height")
    state_path = session / "session.json"
    if args.action not in {"run", "attach"} and not state_path.exists():
        parser.exit(
            1,
            "Session is disconnected. Start run, or ask the user to run attach in another terminal.\n",
        )
    try:
        attached = (
            state_path.exists()
            and json.loads(state_path.read_text()).get("kind") == "attached"
        )
    except (OSError, ValueError, AttributeError) as error:
        parser.exit(1, f"Cannot read session metadata: {error}\n")
    try:
        result = dispatch(session, args, attached=attached)
    except (SessionError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Session {session}: {error}\n")
    raise SystemExit(result)


def dispatch(session: Path, args: argparse.Namespace, *, attached: bool) -> int:
    if args.action == "attach":
        # Desktop portal imports are only needed by the foreground attach owner.
        from framewisp.attach import attach_session

        result = attach_session(session)
    elif args.action == "run":
        command: list[str] = args.command
        if command[:1] == ["--"]:
            command = command[1:]
        result = run_session(
            session,
            command,
            recording=args.record,
            captions=not args.no_captions,
            size=(args.width, args.height),
            x11=args.x11,
        )
    elif args.action in {"record-start", "record-stop", "status", "stop"}:
        if attached and args.action in {"status", "stop"}:
            raise SessionError(
                "status and stop manage headless sessions. Use framewisp --detach to stop desktop attachment."
            )
        result = (
            request_attached(session, args.action, {})
            if attached
            else session_command(
                session,
                args.action,
                args.path if args.action == "record-start" else None,
                captions=not getattr(args, "no_captions", False),
            )
        )
    elif args.action == "screenshot":
        if args.delay:
            time.sleep(args.delay)
        result = (
            request_attached(session, "screenshot", {"path": str(args.path.resolve())})
            if attached
            else screenshot(session, args.path)
        )
    else:
        parameters = {
            key: value
            for key, value in vars(args).items()
            if key not in {"session", "action"}
        }
        with log_input(session, args.action, parameters) as action:
            result = (
                request_attached(session, args.action, parameters)
                if attached
                else perform_input(session, args)
            )
            action.returncode = result
    return result


def perform_input(session: Path, args: argparse.Namespace) -> int:
    if args.action == "move":
        result = send_input(session, ["move", str(args.x), str(args.y)])
    elif args.action == "click":
        result = click_pointer(
            session,
            args.x,
            args.y,
            button=args.button,
            count=args.count,
            modifiers=tuple(args.modifier),
        )
    elif args.action == "drag":
        result = drag_pointer(
            session,
            args.x1,
            args.y1,
            args.x2,
            args.y2,
            duration=args.duration,
            button=args.button,
            modifiers=tuple(args.modifier),
        )
    elif args.action == "scroll":
        result = scroll_pointer(
            session, args.x, args.y, direction=args.direction, steps=args.steps
        )
    elif args.action == "type":
        result = type_text(session, args.text, interval=args.interval)
    else:
        arguments = key_commands(args.chord)
        assert arguments is not None
        result = send_input(session, arguments)
    return result
