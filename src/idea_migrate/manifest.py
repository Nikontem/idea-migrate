"""The record of what a run did, stored alongside its backup.

The manifest is the single source of truth for both undo and the backups
listing, so nothing else in the tool has to keep state.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
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

    # What the run did to Claude Code's per-project data, if anything.
    #
    # All five are defaulted for the same reason jetbrains_root is: a manifest
    # written before this feature existed has none of these keys, and it still
    # has to load and still has to be undoable. Reading such a manifest yields
    # "no Claude root recorded", "no Claude registry recorded", and three
    # empty collections, which every undo route treats as "this run touched
    # nothing under ~/.claude, and did not touch the registry" - a silent
    # no-op rather than an error.
    #
    # claude_root is None, not a guessed path, for the same reason as
    # jetbrains_root: a caller cannot tell a recorded value from an invented
    # one, so the fallback belongs to whoever is doing the undo.
    claude_root: str | None = None
    # [[old_entry_dir, new_entry_dir], ...] - absolute paths, in the order the
    # renames were planned. Lists rather than tuples because that is what JSON
    # round-trips to, and the manifest is compared after a read in the tests.
    claude_renames: list[list[str]] = field(default_factory=list)
    # Post-rename file path -> number of references repaired in it.
    claude_rewritten_files: dict[str, int] = field(default_factory=dict)

    # The Claude Code per-project registry file this run rewrote, if any.
    # None means the run did not touch it - either nothing in it referred to the
    # old location or the file did not exist - and every undo route then leaves
    # it alone. Recorded as a path for the same reason claude_root is: the
    # configuration file can point it somewhere other than ~/.claude.json.
    claude_registry: str | None = None
    # [[old_key, new_key], ...] - the registry keys renamed, in file order.
    claude_registry_renames: list[list[str]] = field(default_factory=list)

    # One record per move, in run order, for a run that moved several
    # directories as one batch. Each record is a plain dict, because that is
    # what JSON round-trips to:
    #
    #   {"source": str, "dest": str,
    #    "status": "pending" | "moved" | "failed",
    #    "rewritten_files": {path: count},
    #    "claude_renames": [[old, new], ...],
    #    "claude_rewritten_files": {path: count},
    #    "registry_renames": [[old_key, new_key], ...]}
    #
    # Empty for a manifest written before batch runs existed; every reader
    # then treats the top-level source/dest and the other top-level fields as
    # the one and only move. For a batch the top-level fields hold the first
    # move's paths and the union of every move's counts and renames, so an
    # older reader still sees something truthful. See ``move_records``.
    moves: list[dict] = field(default_factory=list)
    # Where the IDEs keep their per-project caches, recorded for the same
    # reason jetbrains_root is: the configuration file can point it elsewhere,
    # and undo has to rename directories back inside the tree the run touched.
    # None means the manifest does not say, and every undo route then falls
    # back to its own configuration.
    caches_root: str | None = None
    # [[old_cache_dir, new_cache_dir], ...] - absolute paths, in the order the
    # renames were planned. Only the external module storage is ever in here;
    # see ide_caches for why the rest of the cache tree is left alone.
    cache_renames: list[list[str]] = field(default_factory=list)
    # Post-rename file path -> number of references repaired in it.
    cache_rewritten_files: dict[str, int] = field(default_factory=dict)

    # Destination parent directories the run created because they did not
    # exist, absolute, outermost first. Undo removes the ones that are still
    # empty, innermost first, so a rolled-back run leaves no new directories.
    created_dirs: list[str] = field(default_factory=list)

    def move_records(self) -> list[MoveRecord]:
        """Return every move this run made, oldest manifests included.

        A manifest with no ``moves`` list describes exactly one move in its
        top-level fields, so that is what comes back for it.
        """
        if not self.moves:
            return [
                MoveRecord(
                    source=self.source,
                    dest=self.dest,
                    status=self.move_status,
                    rewritten_files=dict(self.rewritten_files),
                    claude_renames=[list(pair) for pair in self.claude_renames],
                    claude_rewritten_files=dict(self.claude_rewritten_files),
                    registry_renames=[
                        list(pair) for pair in self.claude_registry_renames
                    ],
                    cache_renames=[list(pair) for pair in self.cache_renames],
                    cache_rewritten_files=dict(self.cache_rewritten_files),
                )
            ]
        return [
            MoveRecord(
                source=record["source"],
                dest=record["dest"],
                status=record.get("status", "pending"),
                rewritten_files=dict(record.get("rewritten_files") or {}),
                claude_renames=[
                    list(pair) for pair in record.get("claude_renames") or []
                ],
                claude_rewritten_files=dict(
                    record.get("claude_rewritten_files") or {}
                ),
                registry_renames=[
                    list(pair) for pair in record.get("registry_renames") or []
                ],
                cache_renames=[
                    list(pair) for pair in record.get("cache_renames") or []
                ],
                cache_rewritten_files=dict(
                    record.get("cache_rewritten_files") or {}
                ),
            )
            for record in self.moves
        ]


@dataclass(frozen=True)
class MoveRecord:
    """A typed view of one entry of ``Manifest.moves``."""

    source: str
    dest: str
    status: str
    rewritten_files: dict[str, int]
    claude_renames: list[list[str]]
    claude_rewritten_files: dict[str, int]
    registry_renames: list[list[str]]
    cache_renames: list[list[str]] = field(default_factory=list)
    cache_rewritten_files: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


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

    A manifest written before a field existed simply loads with that field at
    its default - None for ``jetbrains_root``, ``claude_root``,
    ``claude_registry`` and ``caches_root``, empty for the Claude Code and
    IDE-cache collections - so an old backup stays readable and undoable.
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
