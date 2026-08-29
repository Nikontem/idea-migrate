"""Preflight checks and the move itself.

Within one filesystem a move is an instant atomic rename regardless of how large
the directory is. Across filesystems there is no such guarantee, so the fallback
copies, verifies the copy exists, and only then removes the original.
"""

from __future__ import annotations

import os
import shutil

from .errors import IdeRunningError
from .paths import MoveSpec
from .processes import running_ides


def assert_no_ide_running(ps_output: str | None = None) -> None:
    """Raise IdeRunningError if any JetBrains IDE is currently running.

    A running IDE holds its settings in memory and writes them out when it quits,
    which would silently overwrite the repairs this tool makes.
    """
    running = running_ides(ps_output)
    if running:
        names = ", ".join(running)
        raise IdeRunningError(
            f"These JetBrains applications are running: {names}. "
            "Quit them before migrating, or their settings will be overwritten "
            "when they exit."
        )


def move_directory(spec: MoveSpec) -> None:
    """Move the source directory to the destination."""
    if spec.same_device:
        os.rename(spec.source, spec.dest)
        return

    shutil.copytree(spec.source, spec.dest, symlinks=True)
    if not spec.dest.is_dir():
        raise OSError(f"Copy to {spec.dest} did not produce a directory")
    shutil.rmtree(spec.source)
