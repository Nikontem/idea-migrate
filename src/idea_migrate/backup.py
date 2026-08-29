"""Create the backup that makes a run reversible.

Only options/ and workspace/ are copied, because they are the only directories
the tool modifies. Together they are around 36 MB across every installed
product, against 23 GB for the full configuration - the rest is plugins.

The generated undo.sh deliberately does not import this package. It exists for
the case where something has gone wrong, so its only dependency beyond a shell
is python3, for reading the manifest.
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .errors import BackupError

UNDO_SCRIPT_NAME = "undo.sh"
BACKED_UP_SUBDIRS = ("options", "workspace")

UNDO_SCRIPT = r"""#!/usr/bin/env bash
# Roll back one idea-migrate run.
#
# Usage: ./undo.sh [BACKUP_DIR]
# With no argument it uses the directory this script lives in, so it keeps
# working after the backup folder has been moved or renamed.
set -euo pipefail

BACKUP_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
MANIFEST="$BACKUP_DIR/manifest.json"

if [ ! -f "$MANIFEST" ]; then
  echo "No manifest.json found in $BACKUP_DIR" >&2
  echo "Pass the backup directory explicitly: $0 /path/to/backup" >&2
  exit 1
fi

read_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$MANIFEST" "$1"
}

SOURCE="$(read_field source)"
DEST="$(read_field dest)"
UNDONE_AT="$(read_field undone_at)"
HOME_DIR="$(read_field home)"

# Where the IDEs keep their settings. The manifest records it, because the
# tool's configuration file can point it somewhere other than the default.
# Manifests written before that field existed fall back to the default, which
# is where those runs necessarily backed up from.
JETBRAINS="$(read_field jetbrains_root)"
if [ -z "$JETBRAINS" ]; then
  JETBRAINS="$HOME_DIR/Library/Application Support/JetBrains"
fi

if [ -n "$UNDONE_AT" ]; then
  echo "This backup was already rolled back at $UNDONE_AT." >&2
  echo "Refusing to undo it twice." >&2
  exit 1
fi

# Only the IDE executables themselves count as "running". The pattern matches
# an executable basename at the end of a path, which mirrors the IDE_EXECUTABLES
# list in the tool's processes.py so the script and the tool agree.
#
# JetBrains Toolbox and the JetBrains daemon (jetbrainsd) are deliberately NOT
# treated as IDEs. They are an installer/updater and a background helper that
# many people leave running permanently; neither holds IDE settings in memory,
# and matching them would block recovery on a machine where nothing is wrong.
IDE_PATTERN='/(idea|pycharm|webstorm|goland|datagrip|clion|phpstorm|rubymine|rider|rustrover)$'

if pgrep -f "$IDE_PATTERN" >/dev/null 2>&1; then
  echo "A JetBrains IDE appears to be running." >&2
  echo "Quit it before undoing, or its settings will be overwritten on exit." >&2
  exit 1
fi

echo "Rolling back:"
echo "  move   $DEST -> $SOURCE"
echo "  config restored from $BACKUP_DIR/config"
echo

if [ -d "$DEST" ] && [ ! -e "$SOURCE" ]; then
  mv "$DEST" "$SOURCE"
  echo "Moved $DEST back to $SOURCE"
elif [ -e "$SOURCE" ]; then
  echo "Source $SOURCE already exists; leaving the directory alone."
else
  echo "Destination $DEST not found; leaving the directory alone."
fi

if [ -d "$BACKUP_DIR/config" ]; then
  for product_dir in "$BACKUP_DIR"/config/*/; do
    [ -d "$product_dir" ] || continue
    product="$(basename "$product_dir")"
    for sub in options workspace; do
      if [ -d "$product_dir/$sub" ]; then
        mkdir -p "$JETBRAINS/$product/$sub"
        cp -R "$product_dir/$sub/." "$JETBRAINS/$product/$sub/"
      fi
    done
    echo "Restored settings for $product"
  done
fi

python3 - "$MANIFEST" <<'PY'
import datetime, json, sys
path = sys.argv[1]
with open(path) as handle:
    data = json.load(handle)
data["undone_at"] = datetime.datetime.now().isoformat(timespec="seconds")
with open(path, "w") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo
echo "Undo complete."
"""


def new_backup_dir(backup_root: Path, now: datetime) -> Path:
    """Create and return a fresh timestamped directory under the backup root."""
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    candidate = backup_root / stamp
    suffix = 1
    while candidate.exists():
        candidate = backup_root / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def back_up_products(
    product_dirs: Sequence[Path], backup_dir: Path
) -> list[str]:
    """Copy each product's options/ and workspace/ into the backup directory.

    Returns the product names backed up, in the order given.
    """
    config_root = backup_dir / "config"
    names: list[str] = []
    try:
        for product in product_dirs:
            for subdir in BACKED_UP_SUBDIRS:
                origin = product / subdir
                if origin.is_dir():
                    shutil.copytree(origin, config_root / product.name / subdir)
            names.append(product.name)
    except OSError as exc:
        raise BackupError(f"Could not create the backup: {exc}") from exc
    return names


def write_undo_script(backup_dir: Path) -> Path:
    """Write the standalone undo script into a backup directory."""
    script = backup_dir / UNDO_SCRIPT_NAME
    script.write_text(UNDO_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return script
