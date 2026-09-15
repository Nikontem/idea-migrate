"""List the backups on disk.

Everything shown here is read from each backup's own manifest, so the listing
stays correct without the tool keeping state anywhere else.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .backup import UNDO_SCRIPT_NAME
from .manifest import MANIFEST_NAME, Manifest, read_manifest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupSummary:
    directory: Path
    manifest: Manifest
    size_bytes: int


def _directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def find_backups(backup_root: Path) -> list[BackupSummary]:
    """Return every backup under the root, newest first."""
    if not backup_root.is_dir():
        return []

    summaries: list[BackupSummary] = []
    for entry in backup_root.iterdir():
        if not entry.is_dir() or not (entry / MANIFEST_NAME).is_file():
            continue
        try:
            manifest = read_manifest(entry)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Skipping %s: its manifest could not be read (%s).", entry, exc
            )
            continue
        summaries.append(
            BackupSummary(
                directory=entry,
                manifest=manifest,
                size_bytes=_directory_size(entry),
            )
        )
    return sorted(summaries, key=lambda s: s.directory.name, reverse=True)


def format_backups(
    summaries: Sequence[BackupSummary], backup_root: Path
) -> str:
    """Render the backups listing for the terminal."""
    if not summaries:
        return (
            f"No backups found in {backup_root}\n"
            "Backups are created the first time you run a migration."
        )

    lines = [f"Backups in {backup_root}", ""]
    for summary in summaries:
        manifest = summary.manifest
        state = (
            f"already rolled back on {manifest.undone_at}"
            if manifest.undone_at
            else "active"
        )
        lines.append(
            f"  {summary.directory.name}   ({_human_size(summary.size_bytes)}, {state})"
        )
        # One pair of lines per directory the run moved. A run that moved
        # several as one batch is listed in full rather than by its first move,
        # because the listing is what a user reads to decide which backup to
        # roll back, and a batch named only by its first move would look like a
        # run that never touched the other directories.
        #
        # A manifest written before batch runs existed describes one move in
        # its top-level fields, and ``move_records`` returns exactly that, so
        # such a backup is listed as it always was.
        for record in manifest.move_records():
            lines.append(f"    moved:  {record.source}")
            lines.append(f"        ->  {record.dest}")
        lines.append(
            f"    files repaired: {sum(manifest.rewritten_files.values())} "
            f"across {len(manifest.backed_up_products)} products"
        )
        if not manifest.undone_at:
            # Both routes are listed: the subcommand needs the tool installed
            # and working, the script needs only bash and python3.
            lines.append(f"    undo:   idea-migrate undo {summary.directory}")
            lines.append(f"            {summary.directory / UNDO_SCRIPT_NAME}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
