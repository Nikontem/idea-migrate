"""Preflight checks and the move itself.

Within one filesystem a move is an instant atomic rename regardless of how large
the directory is. Across filesystems there is no such guarantee, so the fallback
copies, then compares the two trees entry by entry, and refuses to delete the
source unless they match.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .errors import IdeRunningError, MoveError
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


def _tree_snapshot(root: Path) -> dict[str, tuple[str, object]]:
    """Describe every entry under ``root``, keyed by its relative path.

    lstat is used rather than stat so a symlink is described as a symlink
    rather than as whatever it points at - a move preserves links as links,
    and following them here would compare the wrong thing.
    """
    snapshot: dict[str, tuple[str, object]] = {}
    for path in root.rglob("*"):
        info = path.lstat()
        relative = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("dir", None)
        else:
            snapshot[relative] = ("file", info.st_size)
    return snapshot


def _describe_copy_failure(exc: OSError, limit: int = 3) -> str:
    """Summarise why a copy failed, without printing one line per file.

    ``shutil.copytree`` does not stop at the first problem: it carries on and
    raises a single ``shutil.Error`` at the end holding one entry for every
    file it could not copy. On a large tree that is thousands of lines, which
    buries the part the user has to act on, so only the first few are shown
    and the rest are counted.
    """
    collected = exc.args[0] if isinstance(exc, shutil.Error) and exc.args else None
    if not isinstance(collected, list) or not collected:
        return str(exc)

    shown: list[str] = []
    for entry in collected[:limit]:
        if isinstance(entry, tuple) and len(entry) == 3:
            source, _destination, reason = entry
            shown.append(f"{source}: {reason}")
        else:
            shown.append(str(entry))
    remaining = len(collected) - limit
    if remaining > 0:
        shown.append(f"and {remaining} more file(s)")
    return "First errors: " + "; ".join(shown) + "."


def move_directory(spec: MoveSpec) -> None:
    """Move the source directory to the destination.

    On a cross-device move the copy is verified by comparing the two trees
    entry by entry on path, kind (file, directory or symbolic link) and size -
    not by hashing contents, which on a multi-gigabyte tree would cost more
    than the copy itself. That catches a truncated or missing file, which is
    what an interrupted or out-of-space copy produces; it would not catch
    silent corruption that preserved every file's length.
    """
    if spec.same_device:
        os.rename(spec.source, spec.dest)
        return

    before = _tree_snapshot(spec.source)
    try:
        shutil.copytree(spec.source, spec.dest, symlinks=True)
    except OSError as exc:
        # shutil.Error is a subclass of OSError, so this covers both the
        # per-file collection copytree raises at the end and an outright
        # failure such as a full disk.
        raise MoveError(
            f"Copying {spec.source} to {spec.dest} failed. Nothing was "
            f"deleted, so the source at {spec.source} is still intact and "
            f"complete. A partial copy was left at {spec.dest}: delete it "
            "before retrying, or the retry will stop because the destination "
            f"already exists. {_describe_copy_failure(exc)}"
        ) from exc
    after = _tree_snapshot(spec.dest)

    if before != after:
        missing = sorted(set(before) - set(after))
        changed = sorted(
            key for key in set(before) & set(after) if before[key] != after[key]
        )
        extra = sorted(set(after) - set(before))
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if changed:
            details.append(f"changed: {', '.join(changed)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        raise MoveError(
            f"Copy verification failed for {spec.dest}; leaving the source at "
            f"{spec.source} and the partial copy in place. "
            + "; ".join(details)
        )

    shutil.rmtree(spec.source)
