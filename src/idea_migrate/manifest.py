"""The record of what a run did, stored alongside its backup.

The manifest is the single source of truth for both undo and the backups
listing, so nothing else in the tool has to keep state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


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


def write_manifest(backup_dir: Path, manifest: Manifest) -> Path:
    """Write the manifest into a backup directory and return its path."""
    target = backup_dir / MANIFEST_NAME
    target.write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def read_manifest(backup_dir: Path) -> Manifest:
    """Read the manifest from a backup directory."""
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
