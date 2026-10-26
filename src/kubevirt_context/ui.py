"""Terminal presentation for kvctx.

Every command renders through this module, so the tool has one visual language:
``▸`` step headings for long operations, ``✓``/``!``/``✗`` marks for results,
and aligned label/value blocks for state. Decoration is limited to real
terminals — piped output uses plain status words (``ok:``/``warn:``/``error:``)
and no animation, so scripts and logs stay greppable.

Nothing here holds state: a console is created per command and passed down.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Iterable, Sequence

from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

THEME = Theme({
    "kv.step": "bold cyan",
    "kv.ok": "green",
    "kv.warn": "yellow",
    "kv.err": "bold red",
    "kv.key": "bold",
    "kv.dim": "grey50",
    "kv.path": "cyan",
    "kv.head": "bold cyan",
})

# Decoration on a real terminal, plain words in a pipe.
_GLYPHS = {"step": "▸", "ok": "✓", "warn": "!", "err": "✗", "sub": "·", "arrow": "→"}
_ASCII = {"step": "*", "ok": "ok:", "warn": "warn:", "err": "error:", "sub": "-", "arrow": "->"}

_PIPED_WIDTH = 200


def console(*, stderr: bool = False) -> Console:
    """A console in the shared theme.

    Piped output keeps full lines: no 80-column re-wrapping of paths, no
    colour. Rich drops styles on its own when the stream is not a terminal.
    """
    stream = sys.stderr if stderr else sys.stdout
    # FORCE_COLOR in CI must not turn a pipe into an animated terminal.
    terminal = bool(getattr(stream, "isatty", lambda: False)())
    con = Console(file=stream, theme=THEME, force_terminal=None if terminal else False,
                  highlight=False, emoji=False, markup=False)
    if not con.is_terminal:
        con.width = max(_PIPED_WIDTH, con.width)
    return con


def marks(con: Console) -> dict[str, str]:
    """The glyph set for this console."""
    if con.is_terminal and not con.is_dumb_terminal and not con.options.ascii_only:
        return _GLYPHS
    return _ASCII


# --- result lines ------------------------------------------------------------

def step(con: Console, title: str) -> None:
    """Announce a long-running phase."""
    m = marks(con)
    line = Text()
    line.append(f"{m['step']} ", style="kv.step")
    line.append(title, style="kv.step")
    con.print(line, soft_wrap=not con.is_terminal)


def done(con: Console, text: str) -> None:
    """Report a phase or command that finished."""
    m = marks(con)
    line = Text()
    line.append(f"{m['ok']} ", style="kv.ok")
    line.append(text, style="kv.ok")
    con.print(line, soft_wrap=not con.is_terminal)


def note(con: Console, text: str) -> None:
    """A detail line under the current step."""
    con.print(Text(f"  {text}", style="kv.dim"), soft_wrap=True)


def hint(con: Console, text: str) -> None:
    """A next-step line, shown after a command succeeds."""
    m = marks(con)
    line = Text()
    line.append(f"  {m['arrow']} ", style="kv.dim")
    line.append(text, style="kv.dim")
    con.print(line, soft_wrap=not con.is_terminal)


def info(con: Console, text: str) -> None:
    con.print(Text(text), soft_wrap=True)


def warn(con: Console, text: str) -> None:
    """A recoverable problem; goes to stderr like any diagnostic."""
    err = console(stderr=True)
    con.file.flush()
    m = marks(err)
    line = Text()
    line.append(f"{m['warn']} ", style="kv.warn")
    line.append(text, style="kv.warn")
    err.print(line, soft_wrap=True)


def fail(con: Console, text: str) -> None:
    """A fatal problem; goes to stderr."""
    err = console(stderr=True)
    con.file.flush()
    m = marks(err)
    line = Text()
    line.append(f"{m['err']} ", style="kv.err")
    line.append(text, style="kv.err")
    err.print(line, soft_wrap=True)


# --- state blocks ------------------------------------------------------------

def block(con: Console, title: str, rows: Sequence[tuple[str, str]]) -> None:
    """A titled, aligned label/value block: the way this tool reports state."""
    con.print(Text(title, style="kv.head"))
    if not con.is_terminal:
        width = max((len(key) for key, _ in rows), default=0)
        for key, value in rows:
            con.print(Text(f"  {key:<{width}}  {value}"), soft_wrap=True)
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="kv.key", no_wrap=True, vertical="top")
    grid.add_column(style="", vertical="top", overflow="fold")
    for key, value in rows:
        grid.add_row(Text(f"  {key}"), Text(value))
    con.print(grid)


def columns(
    con: Console,
    rows: Iterable[Sequence[Text | str]],
    *,
    styles: Sequence[str | None] = (),
    no_wrap: Sequence[bool] = (),
    min_widths: Sequence[int] = (),
) -> None:
    """An unbordered grid for lists (skills, MCP targets, checks)."""
    rows = [tuple(row) for row in rows]
    if not rows:
        return
    width = max(len(row) for row in rows)
    grid = Table.grid(padding=(0, 2))
    for index in range(width):
        grid.add_column(
            style=styles[index] if index < len(styles) and styles[index] else "",
            no_wrap=no_wrap[index] if index < len(no_wrap) else False,
            min_width=min_widths[index] if index < len(min_widths) else 0,
            overflow="fold",
        )
    for row in rows:
        grid.add_row(*(Text(value) if isinstance(value, str) else value for value in row))
    con.print(grid)


def paragraph(con: Console, text: str, *, indent: int = 4, style: str = "kv.dim") -> None:
    """A wrapped paragraph with a hanging indent."""
    con.print(Padding(Text(text, style=style), (0, 0, 0, indent)), soft_wrap=False)


# --- formatting --------------------------------------------------------------

def count(n: int) -> str:
    """Thousands-separated counts: 15768 -> '15,768'."""
    return f"{n:,}"


def size(num_bytes: float) -> str:
    """Human-readable byte size with one decimal from MB up."""
    for unit, scale in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num_bytes >= scale:
            decimals = 0 if unit == "KB" else (1 if unit == "MB" else 2)
            return f"{num_bytes / scale:.{decimals}f} {unit}"
    return f"{int(num_bytes)} B"


def duration(seconds: float) -> str:
    """Compact elapsed time: 840ms, 12.4s, 3m12s, 1h04m."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _parse_stamp(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ago(stamp: str | None) -> str:
    """'8m ago' for an ISO-8601 UTC stamp; the raw stamp if unparsable."""
    if not stamp:
        return "unknown"
    moment = _parse_stamp(stamp)
    if moment is None:
        return stamp
    delta = (datetime.now(timezone.utc) - moment).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def local_time(stamp: str | None) -> str:
    """An ISO-8601 UTC stamp rendered in the reader's timezone."""
    if not stamp:
        return "unknown"
    moment = _parse_stamp(stamp)
    if moment is None:
        return stamp
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def shorten_path(path: object, limit: int = 68) -> str:
    """Replace $HOME with ~ and elide the middle of very long paths."""
    text = str(path)
    home = os.path.expanduser("~")
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        text = "~" + text[len(home):]
    if len(text) <= limit:
        return text
    keep = limit - 3
    head = keep // 2
    return f"{text[:head]}...{text[-(keep - head):]}"


# --- help --------------------------------------------------------------------

def command_help(
    con: Console,
    *,
    usage: str,
    summary: str,
    groups: Sequence[tuple[str, Sequence[tuple[str, str]]]] = (),
    options: Sequence[tuple[str, str]] = (),
    epilog: Sequence[str] = (),
) -> None:
    """Render `kvctx --help` (and each subcommand's) in the shared style."""
    con.print(Text("Usage", style="kv.head"))
    con.print(Text(f"  {usage}"))
    if summary:
        con.print()
        con.print(Text(summary), soft_wrap=True)

    for title, rows in groups:
        if not rows:
            continue
        con.print()
        con.print(Text(title, style="kv.head"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="kv.step", no_wrap=True)
        grid.add_column(overflow="fold")
        for name, text in rows:
            grid.add_row(Text(f"  {name}"), Text(text))
        con.print(grid)

    if options:
        con.print()
        con.print(Text("Options", style="kv.head"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="kv.step", no_wrap=True)
        grid.add_column(overflow="fold")
        for name, text in options:
            grid.add_row(Text(f"  {name}"), Text(text))
        con.print(grid)

    if epilog:
        con.print()
        for line in epilog:
            con.print(Text(line), soft_wrap=True)
