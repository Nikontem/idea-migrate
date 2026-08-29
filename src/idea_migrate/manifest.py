"""The record of what a run did, stored alongside its backup.

The manifest is the single source of truth for both undo and the backups
listing, so nothing else in the tool has to keep state.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class Manifest:
    version: int
    tool_version: str
    created_at: str
    home: str
    source: str
    dest: str
    move_status: str
    backed_up_products: list[str]
    rewritten_files: dict[str, int]
    undone_at: str | None
    # Where the IDEs keep their settings. Recorded rather than assumed,
    # because the configuration file can point it somewhere else and every
    # undo route has to restore into the directory the backup was taken from.
    #
    # None means "this manifest does not say" - it was written before the
    # field existed - and each undo route then applies its own fallback. It is
    # left as None rather than filled in with a guess, because a caller that
    # cannot tell a recorded value from an inferred one cannot decide whether
    # to prefer it over its own configuration.
    jetbrains_root: str | None = None


def write_manifest(backup_dir: Path, manifest: Manifest) -> Path:
    """Write the manifest into a backup directory and return its path.

    The write goes to a temporary file in the same directory and is then moved
    into place with os.replace, which is atomic. A crash mid-write therefore
    leaves the previous manifest intact rather than a truncated one - this file
    is what undo and the backups listing depend on, so a torn write would break
    recovery exactly when it is needed.

    If the write fails, the temporary file is removed before the error is
    re-raised. The tool never deletes a backup directory, so anything left
    there stays for good: a leaked temporary file would sit alongside the
    manifest forever and inflate the size reported by the backups listing.
    """
    target = backup_dir / MANIFEST_NAME
    payload = json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=backup_dir, delete=False
    )
    temp_name = handle.name
    try:
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temp_name, target)
    except BaseException:
        # Never leave a stray temporary file behind in the backup directory.
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise
    return target


def read_manifest(backup_dir: Path) -> Manifest:
    """Read the manifest from a backup directory.

    A manifest written before ``jetbrains_root`` existed simply loads with
    that field set to None, so an old backup stays readable and undoable.
    """
    target = backup_dir / MANIFEST_NAME
    if not target.is_file():
        raise FileNotFoundError(f"No {MANIFEST_NAME} in {backup_dir}")
    data = json.loads(target.read_text(encoding="utf-8"))
    return Manifest(**data)


def mark_undone(backup_dir: Path, when: str) -> Manifest:
    """Record that this backup has been rolled back, and persist the change."""
    updated = replace(read_manifest(backup_dir), undone_at=when)
    write_manifest(backup_dir, updated)
    return updated
