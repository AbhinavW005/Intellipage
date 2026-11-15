"""Console presentation helpers for an interactive Windows terminal."""

from __future__ import annotations

import os
import sys


_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_CYAN = "\x1b[96m"
_GREEN = "\x1b[92m"
_YELLOW = "\x1b[93m"
_RED = "\x1b[91m"
_BLUE = "\x1b[94m"


def supports_color() -> bool:
    return bool(sys.stdout.isatty()) and os.getenv("NO_COLOR") is None


def color(text: object, code: str) -> str:
    value = str(text)
    return f"{code}{value}{_RESET}" if supports_color() else value


def title(text: str) -> str:
    return color(f"\n{'=' * 72}\n  {text}\n{'=' * 72}", _BOLD + _CYAN)


def section(text: str) -> str:
    return color(f"\n--- {text} ---", _BOLD + _BLUE)


def success(text: str) -> str:
    return color(text, _GREEN)


def warning(text: str) -> str:
    return color(text, _YELLOW)


def error(text: str) -> str:
    return color(text, _RED)


def menu_header() -> str:
    return title("INTELLIPAGE  |  WINDOWS MEMORY & WORKLOAD CONSOLE")


def pause(message: str = "Press Enter to return to the menu...") -> None:
    if sys.stdin.isatty():
        try:
            input(message)
        except EOFError:
            pass


def clear_screen() -> None:
    if sys.stdout.isatty():
        print("\x1b[2J\x1b[H", end="")
