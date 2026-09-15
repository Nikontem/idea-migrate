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


@dataclass(frozen=True)
class MoveCheck:
    """What checking one move found, whether or not the move is usable.

    A rejected move still comes back with its normalised paths, because a
    caller checking several moves at once has to be able to name the offending
    move in a message even when it is the one it cannot use.

    ``problem`` is the first thing found wrong, worded exactly as
    :func:`validate_move` would raise it, or None when the move is fine.
    ``same_device`` is meaningful only in the latter case.
    """

    source: Path
    dest: Path
    home: Path
    same_device: bool
    problem: str | None = None
    # Destination parent directories that do not exist yet, outermost first.
    # Always empty unless the caller asked for them to be created: without
    # that, a missing parent is a problem rather than a list of work to do.
    missing_parents: tuple[Path, ...] = ()


def _absolute(value: str | Path) -> Path:
    """Expand ``~``, make the path absolute, and collapse ``..`` lexically.

    The path need not exist. Normalisation is lexical rather than via resolve(),
    so a ".."-spelled path does not reach the config rewriter, while symlink
    spellings are preserved - the IDEs recorded whatever spelling the user
    opened the project under, and the rewrite matches on that stored text.
    """
    expanded = Path(os.path.expanduser(str(value))).absolute()
    return Path(os.path.normpath(expanded))


def _same_dir(left: Path, right: Path) -> bool:
    """True when both paths exist and name the same directory."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _is_inside(inner: Path, outer: Path) -> bool:
    """True when ``inner`` is at or below ``outer``.

    ``inner`` need not exist. It is resolved with ``os.path.realpath`` before
    the walk begins, which collapses ``..`` segments and follows symlinks along
    whatever prefix of it does exist, leaving a non-existent tail alone. Walking
    the typed spelling instead would be purely lexical, and containment reached
    through a symlinked parent directory would go unnoticed. Each existing
    ancestor of the resolved path is then compared against ``outer`` with
    samefile, so a case-insensitive filesystem cannot hide a match behind a
    different spelling either.
    """
    candidate = Path(os.path.realpath(inner))
    while True:
        if candidate.exists() and _same_dir(candidate, outer):
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def check_move(
    source: str | Path,
    dest: str | Path,
    home: Path,
    *,
    create_parents: bool = False,
) -> MoveCheck:
    """Check one move and describe what was found, without raising.

    The checks, their order and their wording are :func:`validate_move`'s; the
    only difference is that the first problem comes back in ``problem`` rather
    than being raised. That is what lets a caller checking several moves at once
    report every one of their problems in a single pass instead of stopping at
    the first, which is the whole reason this function exists.

    With ``create_parents`` a destination parent that does not exist is work to
    do rather than a reason to refuse: ``missing_parents`` lists the directories
    that would have to be created, outermost first, and ``same_device`` is
    measured against the first ancestor that does exist. That ancestor still has
    to be a writable directory - nothing can be created underneath a file, or
    inside a directory the user cannot write to - and when it is not, the
    ordinary messages apply.
    """
    src = _absolute(source)
    dst = _absolute(dest)
    home = _absolute(home)

    def refuse(problem: str) -> MoveCheck:
        # same_device is left False rather than measured: the paths a refused
        # move names may not exist, and the field is documented as meaningful
        # only for a move that passed.
        return MoveCheck(
            source=src, dest=dst, home=home, same_device=False, problem=problem
        )

    if not src.exists():
        return refuse(f"Source does not exist: {src}")
    if not src.is_dir():
        return refuse(f"Source is not a directory: {src}")
    if _same_dir(src, home):
        return refuse(f"Refusing to move the home directory itself: {src}")
    # Checked before the existence test below: on a case-insensitive filesystem
    # two spellings of one directory would otherwise be reported as
    # "destination already exists", which tells the user nothing useful. The
    # filesystem is the authority here - if the destination does not exist it
    # cannot be the same directory, and if it does, samefile settles it
    # correctly on case-sensitive and case-insensitive volumes alike.
    if dst.exists() and _same_dir(src, dst):
        return refuse(f"Source and destination are the same directory: {src}")
    # Ahead of the parent checks so that moving a directory into itself is
    # reported as such, rather than as a missing parent directory.
    if _is_inside(dst, src):
        return refuse(
            f"Destination {dst} is inside the source {src}; "
            "a directory cannot be moved into itself."
        )
    # is_symlink as well as exists: exists() follows the link, so a symlink
    # whose target is gone reads as absent even though something really is
    # sitting at that path and the move would fail on it.
    if dst.exists() or dst.is_symlink():
        return refuse(f"Destination already exists: {dst}")

    # The directory the new one will be created in. Without create_parents that
    # is always the destination's own parent; with it, the walk climbs to the
    # first ancestor that is really there, and everything passed on the way up
    # is what would have to be created.
    parent = dst.parent
    missing: tuple[Path, ...] = ()
    if create_parents:
        climbing: list[Path] = []
        # is_symlink as well as exists, for the reason given above: a dangling
        # symlink is not a directory that can be created, it is something
        # already sitting in the way, and mkdir would fail on it.
        while not (parent.exists() or parent.is_symlink()):
            climbing.append(parent)
            if parent.parent == parent:
                break
            parent = parent.parent
        missing = tuple(reversed(climbing))

    if not parent.is_dir():
        return refuse(f"Destination's parent directory does not exist: {dst.parent}")
    if not os.access(parent, os.W_OK):
        return refuse(f"Destination's parent directory is not writable: {parent}")

    # lstat, not stat: if the source is itself a symlink, a rename moves the
    # link entry, which lives on the device holding the link rather than the
    # one holding whatever it points at.
    same_device = os.lstat(src).st_dev == os.stat(parent).st_dev
    return MoveCheck(
        source=src,
        dest=dst,
        home=home,
        same_device=same_device,
        missing_parents=missing,
    )


def validate_move(source: str | Path, dest: str | Path, home: Path) -> MoveSpec:
    """Check a move is safe and return the validated specification.

    Raises PathValidationError, naming the offending path, on the first problem.
    """
    check = check_move(source, dest, home)
    if check.problem is not None:
        raise PathValidationError(check.problem)
    return MoveSpec(
        source=check.source,
        dest=check.dest,
        home=check.home,
        same_device=check.same_device,
    )
