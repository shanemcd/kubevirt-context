"""kvctx command line interface."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import sys
import traceback

from . import __version__, ui, workspace
from .errors import CloneErrors, KvctxError
from .storage import workspace_lock

# name, group, one-line help, usage line. This order is the order shown in
# `kvctx --help`, and the registry is the single source of truth for both the
# parser and the help screen.
_COMMANDS: list[tuple[str, str, str, str]] = [
    ("setup", "Setup and maintenance",
     "clone every repository and build the index",
     "kvctx setup [--only A,B,C] [--workers N]"),
    ("sync", "Setup and maintenance",
     "fast-forward clones, discover new repositories, refresh the index",
     "kvctx sync [--only A,B,C] [--workers N]"),
    ("clones", "Setup and maintenance",
     "clone missing repositories without touching existing ones",
     "kvctx clones [--only A,B,C] [--workers N]"),
    ("index", "Setup and maintenance",
     "build or refresh the CodeGraph index",
     "kvctx index"),
    ("doctor", "Inspect",
     "check prerequisites; read-only, installs nothing",
     "kvctx doctor"),
    ("status", "Inspect",
     "report workspace and index state",
     "kvctx status"),
    ("path", "Inspect",
     "print the workspace directory",
     "kvctx path"),
    ("skills", "Configure",
     "list, install, or uninstall the bundled agent skills",
     "kvctx skills [list|install|uninstall] [DIR] [--force]"),
    ("clean", "Housekeeping",
     "remove the owned workspace; leave agent configuration alone",
     "kvctx clean"),
]

_GROUPS = list(dict.fromkeys(group for _name, group, _text, _usage in _COMMANDS))
_BY_NAME = {name: (group, text, usage) for name, group, text, usage in _COMMANDS}


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _repo_list(value: str) -> str:
    names = [name.strip() for name in value.split(",")]
    if not names or any(not name or not workspace._NAME_RE.fullmatch(name) or name in (".", "..") for name in names):
        raise argparse.ArgumentTypeError("expected a non-empty comma-separated list of repository names")
    return ",".join(dict.fromkeys(names))


def _clone_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--only", metavar="A,B,C", default=None, type=_repo_list,
        help="clone only these repositories (a later plain `sync` grows to the whole organization)",
    )
    options.add_argument(
        "--workers", type=_positive_int, default=4, metavar="N",
        help="concurrent git operations (default 4)",
    )
    return options


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        self.command_parsers = {}
        self.help_command = None
        self.root_parser = self
        super().__init__(*args, **kwargs)

    def print_help(self, file=None) -> None:
        _print_help(self.root_parser, self.help_command)

    def error(self, message: str) -> None:
        con = ui.console()
        ui.fail(con, message)
        ui.console(stderr=True).print(f"Try `{self.prog} --help` for usage.")
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    clone_options = _clone_options()

    parser = ArgumentParser(
        prog="kvctx", add_help=False,
        description="KubeVirt research workspace: every kubevirt repository shallow-cloned "
                    "and indexed into one CodeGraph, plus read-only GitHub history.",
    )
    parser.add_argument("-h", "--help", action="help", help="show this help and exit")
    parser.add_argument("--version", action="version", version=f"kvctx {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    parents = {"setup", "clones", "sync"}
    for name, _group, help_text, _usage in _COMMANDS:
        child = sub.add_parser(
            name, add_help=False,
            parents=[clone_options] if name in parents else [],
            help=help_text, description=help_text,
        )
        child.root_parser = parser
        child.help_command = name
        parser.command_parsers[name] = child
        child.add_argument("-h", "--help", action="help", help="show this help and exit")
        if name == "skills":
            child.add_argument("action", nargs="?", choices=("list", "install", "uninstall"), default="list",
                               help="list the bundled skills (default), install, or uninstall them")
            child.add_argument("dest", nargs="?", default=None,
                               help="target directory for install/uninstall (default ~/.agents/skills)")
            child.add_argument("--force", action="store_true",
                               help="overwrite existing skills on install; remove modified owned skills on uninstall")

    return parser


def _option_rows(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """Two-column rows for a parser's options and positionals."""
    rows: list[tuple[str, str]] = []
    has_commands = bool(getattr(parser, "command_parsers", {}))
    for action in parser._actions:  # noqa: SLF001
        if action.dest == "help" and not action.option_strings:
            continue
        if not action.option_strings:
            if has_commands:
                continue  # the Commands section lists the subcommands already
            rows.append((f"<{action.metavar or action.dest}>", action.help or ""))
            continue
        name = ", ".join(action.option_strings)
        if action.nargs != 0:
            name += f" {action.metavar or action.dest.upper()}"
        rows.append((name, action.help or ""))
    return rows


def _print_help(parser: argparse.ArgumentParser, command: str | None) -> None:
    con = ui.console()

    if command is None:
        groups = []
        for title in _GROUPS:
            rows = [(name, text) for name, group, text, _usage in _COMMANDS if group == title]
            groups.append((title, rows))
        ui.command_help(
            con,
            usage="kvctx <command> [options]",
            summary=parser.description or "",
            groups=groups,
            options=_option_rows(parser),
            epilog=[
                "Examples",
                "  kvctx setup --only kubevirt,containerized-data-importer",
                "  kvctx status",
                "  kvctx skills list",
                "",
                "Docs: https://github.com/codingben/kubevirt-context",
            ],
        )
        return

    _group, summary, usage = _BY_NAME.get(command, ("", "", f"kvctx {command}"))
    child = parser.command_parsers[command]
    ui.command_help(
        con,
        usage=usage,
        summary=summary,
        options=_option_rows(child),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        raise
    if args.command == "skills" and args.action == "list" and (args.dest or args.force):
        parser.command_parsers["skills"].error("DIR and --force are only valid with `skills install` or `skills uninstall`")

    if args.command is None:
        _print_help(parser, None)
        return 0

    try:
        mutating = args.command in {"setup", "sync", "clones", "index", "clean"}
        lock = workspace_lock(workspace.home_dir()) if mutating else nullcontext()
        with workspace.signal_handlers(), lock:
            return _dispatch(args)
    except KeyboardInterrupt:
        ui.fail(ui.console(), "Interrupted.")
        return 130
    except CloneErrors as exc:
        con = ui.console()
        ui.fail(con, f"{len(exc.errors)} repository operation(s) failed; details below")
        for error in exc.errors:
            traceback.print_exception(type(error), error, error.__traceback__)
        return 1
    except KvctxError as exc:
        ui.fail(ui.console(), str(exc))
        return 1
    except OSError as exc:
        # Expected filesystem / executable failures need context, not a stack.
        ui.fail(ui.console(), f"{exc.strerror or type(exc).__name__}" + (f": {exc.filename}" if exc.filename else ""))
        return 1
    except Exception:
        traceback.print_exc()
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        workspace.doctor()
    elif args.command == "setup":
        workspace.setup(only=args.only, workers=args.workers)
    elif args.command == "clones":
        workspace.clones(only=args.only, workers=args.workers)
    elif args.command == "sync":
        workspace.sync(only=args.only, workers=args.workers)
    elif args.command == "index":
        workspace.index()
    elif args.command == "status":
        workspace.status()
    elif args.command == "skills":
        if args.action == "list":
            workspace.skills_list()
        elif args.action == "install":
            workspace.skills_install(args.dest, args.force)
        else:
            workspace.skills_uninstall(args.dest, args.force)
    elif args.command == "clean":
        workspace.clean()
    elif args.command == "path":
        print(workspace.home_dir())
    return 0


if __name__ == "__main__":
    sys.exit(main())
