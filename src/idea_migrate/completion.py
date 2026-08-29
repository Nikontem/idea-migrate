"""Terminal-style tab completion for filesystem paths.

Three details matter here, and each one silently breaks completion if wrong:

* readline treats "/" and "~" as word delimiters by default, so the completer
  would only ever see the fragment after the last slash. The delimiter set is
  narrowed to whitespace so the whole path arrives as one token.
* GNU readline and the libedit build that ships with some macOS Pythons need
  different key bindings for Tab, and each ignores the other's form.
* Completions re-attach the directory prefix exactly as the user typed it, so a
  path beginning with "~" stays that way instead of expanding on screen.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import MigrateError

try:  # pragma: no cover - exercised by whichever branch the platform provides
    import readline
except ImportError:  # pragma: no cover
    readline = None  # type: ignore[assignment]


def path_candidates(text: str) -> list[str]:
    """Return the directory completions for a partially typed path.

    Only directories are offered, because every prompt that uses this asks for
    one. The returned strings keep whatever directory prefix the user typed,
    so "~/" stays "~/" rather than expanding to an absolute path on screen,
    and each comes back with a trailing separator so typing can continue
    straight into the next component.
    """
    typed_dir, partial = os.path.split(text)
    search_dir = Path(os.path.expanduser(typed_dir or "."))

    if not search_dir.is_dir():
        return []

    # Re-attached to every candidate so the display keeps the user's own prefix.
    prefix = typed_dir + "/" if typed_dir and not typed_dir.endswith("/") else typed_dir

    lowered = partial.lower()
    candidates: list[str] = []
    try:
        entries = sorted(search_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    for entry in entries:
        if not entry.name.lower().startswith(lowered):
            continue
        if not entry.is_dir():
            continue
        candidates.append(f"{prefix}{entry.name}/")
    return candidates


class PathCompleter:
    """A readline completer over filesystem paths."""

    def __init__(self) -> None:
        self._cache_text: str | None = None
        self._cache: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        """Return the ``state``-th completion for ``text``, or None when exhausted."""
        if state == 0 or text != self._cache_text:
            self._cache_text = text
            self._cache = path_candidates(text)
        if state < len(self._cache):
            return self._cache[state]
        return None


def enable_path_completion() -> bool:
    """Install the path completer on readline.

    Returns False when readline is unavailable, so the caller can fall back to a
    plain prompt rather than failing.
    """
    if readline is None:
        return False

    readline.set_completer(PathCompleter().complete)
    # Without this, "/" and "~" are treated as word boundaries and the completer
    # receives only the trailing fragment.
    readline.set_completer_delims(" \t\n")

    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    return True


def prompt_for_path(prompt: str) -> Path:
    """Ask the user for a path, with tab completion when it is available."""
    enable_path_completion()
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError as exc:
            # End of input means there is no one there to answer: the tool is
            # being run non-interactively without the paths it needs. Ctrl-C
            # is deliberately not caught here. It is a person cancelling on
            # purpose, and it belongs to main(), which prints a plain
            # "Cancelled." and exits 130. Turning it into this message would
            # report a deliberate cancel as a non-interactive failure and
            # advise a fix for a problem the user does not have.
            raise MigrateError(
                "No input available - pass --source and --dest explicitly "
                "when running non-interactively."
            ) from exc
        if raw:
            return Path(os.path.expanduser(raw)).absolute()
        print("Please enter a path.")
