"""Roll back one migration run.

This is the Python route. Every backup also carries a standalone undo.sh that
does the same job without importing this package, for the case where the tool
itself is what broke.

Undoing never deletes the backup.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .errors import UndoError
from .manifest import MANIFEST_NAME, mark_undone, read_manifest
from .mover import assert_no_ide_running


def undo_backup(
    backup_dir: Path,
    jetbrains_root: Path,
    now: datetime,
    ps_output: str | None = None,
) -> list[str]:
    """Reverse the run recorded in ``backup_dir``. Returns what was done."""
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
    source = Path(manifest.source)
    dest = Path(manifest.dest)

    if dest.is_dir() and not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), str(source))
        done.append(f"Moved {dest} back to {source}")
    elif source.exists():
        done.append(f"Source {source} already exists; left the directory alone.")
    else:
        done.append(f"Destination {dest} not found; left the directory alone.")

    config_root = backup_dir / "config"
    if config_root.is_dir():
        for product_backup in sorted(config_root.iterdir()):
            if not product_backup.is_dir():
                continue
            for subdir in ("options", "workspace"):
                saved = product_backup / subdir
                if not saved.is_dir():
                    continue
                target = jetbrains_root / product_backup.name / subdir
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(saved, target, dirs_exist_ok=True)
            done.append(f"Restored settings for {product_backup.name}")

    mark_undone(backup_dir, now.isoformat())
    done.append(f"Backup kept at {backup_dir}")
    return done
