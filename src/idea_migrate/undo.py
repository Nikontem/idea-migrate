"""Roll back one migration run.

This is the Python route. Every backup also carries a standalone undo.sh that
does the same job without importing this package, for the case where the tool
itself is what broke.

Undoing never deletes the backup.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from .backup import BACKED_UP_SUBDIRS, REGISTRY_BACKUP_NAME
from .errors import UndoError
from .ide_caches import rewrite_caches
from .manifest import MANIFEST_NAME, Manifest, mark_undone, read_manifest
from .mover import assert_no_ide_running
from .rewrite import prefix_variants


def _move_directories_back(manifest: Manifest) -> list[str]:
    """Put every directory this run moved back where it came from.

    A run can move several directories as one batch, and they are reversed in
    the opposite order to the one they were moved in. That matters when one
    move's destination sat under another move's source: undoing the outer move
    first would carry the inner directory back with it, and the inner reversal
    would then find nothing where it expected something.

    A manifest written before batch runs existed describes exactly one move in
    its top-level fields, and ``move_records`` hands that back as a single
    record, so such a backup is reversed exactly as it always was.

    What is on disk decides which of the four outcomes each record gets; the
    recorded status only chooses the wording. A run interrupted with Ctrl-C can
    leave a record saying "pending" for a directory that did in fact move, so
    trusting the status over the filesystem would skip a move that needs
    reversing. The status is consulted for one case only: a move that never
    started - still at its source, nothing at its destination - is the expected
    aftermath of a partial failure rather than an anomaly, and saying so is
    less alarming than reporting the destination as missing.
    """
    done: list[str] = []
    for record in reversed(manifest.move_records()):
        source, dest = Path(record.source), Path(record.dest)
        if dest.is_dir() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(source))
            done.append(f"Moved {dest} back to {source}")
        elif (
            record.status == "pending"
            and source.exists()
            and not dest.exists()
        ):
            done.append(
                f"Move {source} -> {dest} was never started; nothing to reverse."
            )
        elif source.exists():
            done.append(f"Source {source} already exists; left the directory alone.")
        else:
            done.append(f"Destination {dest} not found; left the directory alone.")
    return done


def _remove_created_dirs(manifest: Manifest) -> list[str]:
    """Remove the destination parents the run created, innermost first.

    Only empty ones go, so a rolled-back run leaves no new directories behind
    without ever deleting anything a user has since put there. Innermost first
    because the recorded order is outermost first, and a parent cannot be empty
    until its child is gone.

    A directory that is missing, no longer empty, or not removable is skipped
    without a word: none of those is a failure of the rollback, and the moves
    and file restores that matter have already happened by this point.
    """
    done: list[str] = []
    for name in reversed(manifest.created_dirs):
        directory = Path(name)
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                os.rmdir(directory)
                done.append(f"Removed {directory}")
        except OSError:
            continue
    return done


def _restore_claude_data(backup_dir: Path, manifest: Manifest) -> list[str]:
    """Put Claude Code's per-project data back. Returns what was done.

    Three steps, mirroring the three the migration took. Each renamed entry
    directory is renamed back, in reverse order; then the saved copies of the
    files that were rewritten are merged over what is on disk now - the same
    merge the IDE settings get, for the same reason: a transcript written after
    the migration is not this run's to delete; then the per-project registry is
    put back from its saved copy.

    The registry is the one thing restored whole rather than merged, because it
    was rewritten whole: a key rename is applied to the parsed document and the
    document is written out again, so the only reversal that can be trusted is
    the original bytes. That does discard whatever Claude Code wrote to the
    registry between the migration and the undo, which is the same trade the
    rewritten transcripts make.

    Nothing here is forced. An entry whose new name is no longer a directory, or
    whose old name has since been taken by something else, is reported and
    skipped: the situation is not the one this backup describes, and renaming
    over it would destroy whatever is there.

    A manifest written before this feature has no renames recorded, no recorded
    registry and no ``claude`` directory in its backup, so this is a silent
    no-op for it.
    """
    # Where Claude Code keeps its data. The manifest records it because the
    # configuration file can point it elsewhere; a manifest that predates the
    # field necessarily used the default, which is where that run wrote.
    claude_root = (
        Path(manifest.claude_root)
        if manifest.claude_root
        else Path(manifest.home) / ".claude"
    )

    done: list[str] = []
    for old_name, new_name in reversed(manifest.claude_renames):
        old, new = Path(old_name), Path(new_name)
        if new.is_dir() and not old.exists():
            os.rename(new, old)
            done.append(f"Renamed Claude Code entry {new} back to {old}")
        elif old.exists():
            done.append(f"Claude Code entry {old} already exists; left it alone.")
        else:
            done.append(f"Claude Code entry {new} not found; left it alone.")

    saved = backup_dir / "claude"
    if saved.is_dir():
        shutil.copytree(saved, claude_root, dirs_exist_ok=True)
        done.append(f"Restored Claude Code files from {saved}")

    # Where the registry lives is taken from the manifest and never guessed:
    # the configuration file can point it somewhere other than ~/.claude.json,
    # and a run that renamed no keys recorded nothing here, which is what makes
    # this a no-op rather than a restore into a default location this run never
    # touched. copy2 puts the saved permission bits back along with the bytes.
    saved_registry = backup_dir / REGISTRY_BACKUP_NAME
    if manifest.claude_registry and saved_registry.is_file():
        registry = Path(manifest.claude_registry)
        shutil.copy2(saved_registry, registry)
        done.append(f"Restored Claude Code project registry {registry}")
    return done


def _restore_ide_caches(manifest: Manifest) -> list[str]:
    """Put the IDE's stored module definitions back. Returns what was done.

    Each cache directory is renamed back to the name the source path hashes to,
    and the one absolute path recorded inside it is rewritten the other way -
    the exact reverse of what the migration did, which is what makes the pair
    byte-for-byte reversible.

    Renaming back before rewriting mirrors the forward order, so a run
    interrupted between the two steps is in the same shape either way round.

    Nothing here is forced, for the same reason nothing in the Claude Code
    restore is: a directory that is no longer there, or an old name something
    else has since taken, means the situation is not the one this backup
    describes. A manifest written before this feature records no renames and
    this is a silent no-op for it.
    """
    home = Path(manifest.home)
    done: list[str] = []
    for record in reversed(manifest.move_records()):
        # Old and new swapped: undo rewrites the destination back to the source.
        variants = prefix_variants(Path(record.dest), Path(record.source), home)
        for old_name, new_name in reversed(record.cache_renames):
            old, new = Path(old_name), Path(new_name)
            if new.is_dir() and not old.exists():
                os.rename(new, old)
                rewrite_caches([old], variants)
                done.append(f"Renamed IDE cache {new} back to {old}")
            elif old.exists():
                done.append(f"IDE cache {old} already exists; left it alone.")
            else:
                done.append(f"IDE cache {new} not found; left it alone.")
    return done


def undo_backup(
    backup_dir: Path,
    jetbrains_root: Path,
    now: datetime,
    ps_output: str | None = None,
) -> list[str]:
    """Reverse the run recorded in ``backup_dir``. Returns what was done.

    Settings are restored by overwriting the files captured in the backup;
    any file an IDE created after the migration but before this undo is left
    in place rather than deleted, so the restore is a merge, not a
    byte-exact replacement of the settings directory.
    """
    if not (backup_dir / MANIFEST_NAME).is_file():
        raise UndoError(f"No {MANIFEST_NAME} found in {backup_dir}")

    manifest = read_manifest(backup_dir)
    if manifest.undone_at:
        raise UndoError(
            f"This backup was already rolled back on {manifest.undone_at}. "
            "Refusing to undo it twice."
        )

    assert_no_ide_running(ps_output)

    done: list[str] = []
    done.extend(_move_directories_back(manifest))
    done.extend(_restore_claude_data(backup_dir, manifest))
    done.extend(_restore_ide_caches(manifest))

    config_root = backup_dir / "config"
    if config_root.is_dir():
        for product_backup in sorted(config_root.iterdir()):
            if not product_backup.is_dir():
                continue
            for subdir in BACKED_UP_SUBDIRS:
                saved = product_backup / subdir
                if not saved.is_dir():
                    continue
                target = jetbrains_root / product_backup.name / subdir
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(saved, target, dirs_exist_ok=True)
            done.append(f"Restored settings for {product_backup.name}")

    done.extend(_remove_created_dirs(manifest))

    mark_undone(backup_dir, now.isoformat())
    done.append(f"Backup kept at {backup_dir}")
    return done
