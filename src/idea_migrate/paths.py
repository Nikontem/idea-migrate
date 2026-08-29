"""Validate a source and destination pair before anything is moved.

The filesystem on macOS is case-insensitive, so comparing path strings is not a
reliable way to ask whether two paths name the same directory. Wherever both
paths exist, this module asks the filesystem instead, via os.path.samefile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PathValidationError


@dataclass(frozen=True)
class MoveSpec:
    source: Path
    dest: Path
    home: Path
    same_device: bool


def _absolute(value: str | Path) -> Path:
    """Expand ``~`` and make the path absolute without requiring it to exist."""
    return Path(os.path.expanduser(str(value))).absolute()


def _same_dir(left: Path, right: Path) -> bool:
    """True when both paths exist and name the same directory."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _is_inside(inner: Path, outer: Path) -> bool:
    """True when ``inner`` is at or below ``outer``.

    ``inner`` need not exist. Each of its existing ancestors is compared against
    ``outer`` with samefile, so a case-insensitive filesystem cannot hide a
    match behind a different spelling.
    """
    candidate = inner
    while True:
        if candidate.exists() and _same_dir(candidate, outer):
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def validate_move(source: str | Path, dest: str | Path, home: Path) -> MoveSpec:
    """Check a move is safe and return the validated specification.

    Raises PathValidationError, naming the offending path, on the first problem.
    """
    src = _absolute(source)
    dst = _absolute(dest)
    home = _absolute(home)

    if not src.exists():
        raise PathValidationError(f"Source does not exist: {src}")
    if not src.is_dir():
        raise PathValidationError(f"Source is not a directory: {src}")
    if _same_dir(src, home):
        raise PathValidationError(
            f"Refusing to move the home directory itself: {src}"
        )
    # Checked before the existence test below: on a case-insensitive filesystem
    # two spellings of one directory would otherwise be reported as
    # "destination already exists", which tells the user nothing useful. The
    # filesystem is the authority here - if the destination does not exist it
    # cannot be the same directory, and if it does, samefile settles it
    # correctly on case-sensitive and case-insensitive volumes alike.
    if dst.exists() and _same_dir(src, dst):
        raise PathValidationError(
            f"Source and destination are the same directory: {src}"
        )
    # Ahead of the parent checks so that moving a directory into itself is
    # reported as such, rather than as a missing parent directory.
    if _is_inside(dst, src):
        raise PathValidationError(
            f"Destination {dst} is inside the source {src}; "
            "a directory cannot be moved into itself."
        )
    if dst.exists():
        raise PathValidationError(f"Destination already exists: {dst}")
    if not dst.parent.is_dir():
        raise PathValidationError(
            f"Destination's parent directory does not exist: {dst.parent}"
        )
    if not os.access(dst.parent, os.W_OK):
        raise PathValidationError(
            f"Destination's parent directory is not writable: {dst.parent}"
        )

    same_device = os.stat(src).st_dev == os.stat(dst.parent).st_dev
    return MoveSpec(source=src, dest=dst, home=home, same_device=same_device)
