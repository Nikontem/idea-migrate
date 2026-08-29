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
        lines.extend(
            [
                f"  {summary.directory.name}   ({_human_size(summary.size_bytes)}, {state})",
                f"    moved:  {manifest.source}",
                f"        ->  {manifest.dest}",
                f"    files repaired: {sum(manifest.rewritten_files.values())} "
                f"across {len(manifest.backed_up_products)} products",
            ]
        )
        if not manifest.undone_at:
            lines.append(f"    undo:   {summary.directory / UNDO_SCRIPT_NAME}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
