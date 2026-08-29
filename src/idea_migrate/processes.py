"""Detect running JetBrains IDEs.

A running IDE holds its configuration in memory and flushes it on exit, which
would silently undo our repairs. So the tool refuses to run while one is alive.

JetBrains Toolbox is deliberately excluded: it is an installer and updater that
many people leave running permanently, and treating it as an IDE would block the
tool for no reason.
"""

from __future__ import annotations

import re
import subprocess

# Maps the executable basename inside the app bundle to the product's display
# name. Matching the basename rather than the bundle name keeps this working for
# IDEs installed through Toolbox, whose bundle paths are deeply nested.
IDE_EXECUTABLES = {
    "idea": "IntelliJ IDEA",
    "pycharm": "PyCharm",
    "webstorm": "WebStorm",
    "goland": "GoLand",
    "datagrip": "DataGrip",
    "clion": "CLion",
    "phpstorm": "PhpStorm",
    "rubymine": "RubyMine",
    "rider": "Rider",
    "rustrover": "RustRover",
}

_EXECUTABLE_PATTERN = re.compile(
    r"/(?P<name>" + "|".join(IDE_EXECUTABLES) + r")$", re.IGNORECASE
)


def _read_process_table() -> str:
    """Return one executable path per line for every running process."""
    result = subprocess.run(
        ["ps", "-Ao", "comm="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def running_ides(ps_output: str | None = None) -> list[str]:
    """Return the display names of JetBrains IDEs that are currently running.

    ``ps_output`` accepts pre-captured ``ps -Ao comm=`` text so tests can run
    against fixtures instead of the real process table.
    """
    if ps_output is None:
        ps_output = _read_process_table()

    found: set[str] = set()
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _EXECUTABLE_PATTERN.search(line)
        if match:
            found.add(IDE_EXECUTABLES[match.group("name").lower()])
    return sorted(found)
