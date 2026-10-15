"""Terminal UX: arrow-key line editing, persistent history, ANSI styling.

Colors are emitted only when stdout is a TTY and neither ``NO_COLOR`` nor a
dumb ``TERM`` is set, so pipes, logs, and tests always see plain text.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
BRIGHT_CYAN = "\x1b[96m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
PROMPT_BG = "\x1b[1;37;42m"  # bold white on green: the input prompt block


def color_enabled(stream=None) -> bool:
    """True only when styling is safe: TTY output, no NO_COLOR/dumb TERM."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def paint(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI codes, or return it unchanged when color is off."""
    if not codes or not color_enabled():
        return text
    return "".join(codes) + text + RESET


def banner(text: str) -> str:
    """Bold bright-cyan header block (section titles, summary heading)."""
    return paint(text, BOLD, BRIGHT_CYAN)


def glow(text: str) -> str:
    """Bold default-foreground body (assistant summary / answers)."""
    return paint(text, BOLD)


def error_text(text: str) -> str:
    return paint(text, RED)


def warn_text(text: str) -> str:
    return paint(text, YELLOW)


def hint_text(text: str) -> str:
    return paint(text, DIM)


def prompt_text() -> str:
    """The REPL input prompt: highlighted block on color terminals."""
    if not color_enabled():
        return "you › "
    return paint(" you › ", PROMPT_BG) + " "


def setup_readline(history_file: str | Path, commands: tuple[str, ...] = ()) -> bool:
    """Enable arrow-key editing, Up-history, and TAB command completion.

    History persists in *history_file*. Returns False (plain input still
    works) when readline is unavailable, e.g. on Windows.
    """
    try:
        import readline
    except ImportError:
        return False
    try:
        history_file = Path(history_file)
        try:
            readline.read_history_file(str(history_file))
        except (FileNotFoundError, OSError):
            pass
        readline.set_history_length(200)
        if commands:
            names = tuple(commands)

            def _complete(text: str, state: int):
                buffer = readline.get_line_buffer()
                # Complete command names only at the start of the line.
                if buffer.strip() and (" " in buffer.strip() or "\t" in buffer.strip()):
                    return None
                matches = [c for c in names if c.startswith(text.lower())]
                return matches[state] if state < len(matches) else None

            readline.set_completer(_complete)
            readline.parse_and_bind("tab: complete")

        import atexit

        def _save() -> None:
            try:
                history_file.parent.mkdir(parents=True, exist_ok=True)
                readline.write_history_file(str(history_file))
            except OSError:
                pass

        atexit.register(_save)
        return True
    except Exception:
        return False
