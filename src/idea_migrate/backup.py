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
# The saved copy of Claude Code's per-project registry, at the top of the backup
# directory rather than under claude/. That directory mirrors the layout below
# claude_root, and the registry file sits beside claude_root rather than inside
# it, so it has nowhere to go in that tree without inventing a place for it.
REGISTRY_BACKUP_NAME = "claude-registry.json"

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
# an executable basename at the end of a path, mirroring the IDE_EXECUTABLES
# list in the tool's processes.py so the script and the tool agree.
#
# It has to be `ps -Ao comm=`, not `pgrep -f`, and the difference is not
# cosmetic. `ps -Ao comm=` prints one executable path per line with the
# arguments stripped, which is exactly what processes.py reads, so anchoring
# the basename with "$" means what it looks like it means. `pgrep -f` matches
# the whole command line including arguments, so the same anchored pattern
# would only ever match an IDE started with no arguments at all - and the
# Toolbox shell shim launches IntelliJ as `idea /path/to/project`. The guard
# would then stay silent while an IDE was running, let the rollback proceed,
# and the IDE would flush its in-memory settings over the restored files when
# it quit. That is a worse failure than refusing to run, and it is on the one
# code path that exists for when something has already gone wrong.
#
# JetBrains Toolbox and the JetBrains daemon (jetbrainsd) are deliberately NOT
# treated as IDEs. They are an installer/updater and a background helper that
# many people leave running permanently; neither holds IDE settings in memory,
# and matching them would block recovery on a machine where nothing is wrong.
IDE_PATTERN='/(idea|pycharm|webstorm|goland|datagrip|clion|phpstorm|rubymine|rider|rustrover)$'

# The match is captured and then tested, rather than written as
# `if ps -Ao comm= | grep -Eqi ...`. Under `set -o pipefail` that shorter form
# is actively unsafe: `grep -q` exits as soon as it matches, ps is still
# writing (the table is around 48 KB against a 16 KB pipe buffer), ps dies of
# SIGPIPE, and pipefail reports the pipeline as 141 even though grep matched.
# The `if` then reads a running IDE as "nothing running" - the one wrong
# answer that matters. Capturing the output makes grep drain ps first, and
# `|| true` keeps a no-match, which is grep exit 1, from tripping `set -e`.
RUNNING_IDES="$(ps -Ao comm= | grep -Ei "$IDE_PATTERN" || true)"

if [ -n "$RUNNING_IDES" ]; then
  echo "A JetBrains IDE appears to be running:" >&2
  echo "$RUNNING_IDES" >&2
  echo "Quit it before undoing, or its settings will be overwritten on exit." >&2
  exit 1
fi

# How many Claude Code project entries this run renamed. Read as a count
# rather than as the list itself, so an old manifest with no such key reports
# "0" and the summary simply omits the line.
CLAUDE_ENTRIES="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("claude_renames") or []))' "$MANIFEST")"

# The Claude Code registry file this run rewrote, and how many of its keys were
# renamed. The path is what decides whether the line is printed at all: a
# manifest written before this feature has no such key, read_field gives an
# empty string, and the summary says nothing about a registry.
CLAUDE_REGISTRY="$(read_field claude_registry)"
CLAUDE_REGISTRY_KEYS="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("claude_registry_renames") or []))' "$MANIFEST")"

# How many IDE module caches this run renamed. Counted the same way and for the
# same reason as the Claude Code entries: an old manifest has no such key,
# reports "0", and the summary omits the line.
CACHE_DIRS="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("cache_renames") or []))' "$MANIFEST")"

echo "Rolling back:"
# One line per directory this run moved, in the order they are about to be
# reversed - which is the reverse of the order they were moved in. A run that
# moved a single directory has no "moves" list in older manifests, and none is
# needed: the top-level source and dest describe that one move.
python3 - "$MANIFEST" <<'PY'
import json, sys

with open(sys.argv[1]) as handle:
    data = json.load(handle)

moves = data.get("moves") or [
    {"source": data.get("source"), "dest": data.get("dest")}
]
for record in reversed(moves):
    print("  move   " + str(record.get("dest")) + " -> " + str(record.get("source")))
PY
echo "  config restored from $BACKUP_DIR/config"
if [ "$CLAUDE_ENTRIES" != "0" ]; then
  echo "  claude $CLAUDE_ENTRIES project entries renamed back"
fi
if [ -n "$CLAUDE_REGISTRY" ]; then
  echo "  claude project registry restored ($CLAUDE_REGISTRY_KEYS keys)"
fi
if [ "$CACHE_DIRS" != "0" ]; then
  echo "  caches $CACHE_DIRS IDE module caches renamed back"
fi
echo

# Every directory this run moved, put back in the reverse of the order it was
# moved in. Reverse order matters when one move's destination sat inside
# another move's source: undoing the outer move first would carry the inner
# directory along with it, and the inner reversal would then find nothing where
# it expected something.
#
# What is on disk decides what happens to each record; the recorded status only
# chooses the wording. A run stopped with Ctrl-C can leave a record saying
# "pending" for a directory that did move, so believing the status over the
# filesystem would skip a move that still needs reversing.
#
# This is python3 rather than `mv` because the source's parent may have to be
# recreated first, and because the fallback for a manifest with no "moves" key
# has to be read from JSON anyway.
python3 - "$MANIFEST" <<'PY'
import json, os, shutil, sys

with open(sys.argv[1]) as handle:
    data = json.load(handle)

moves = data.get("moves") or [
    {
        "source": data.get("source"),
        "dest": data.get("dest"),
        "status": data.get("move_status"),
    }
]

for record in reversed(moves):
    source = str(record.get("source") or "")
    dest = str(record.get("dest") or "")
    status = record.get("status") or "pending"
    if os.path.isdir(dest) and not os.path.exists(source):
        parent = os.path.dirname(source)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(dest, source)
        print("Moved " + dest + " back to " + source)
    elif (
        status == "pending"
        and os.path.exists(source)
        and not os.path.exists(dest)
    ):
        print(
            "Move " + source + " -> " + dest
            + " was never started; nothing to reverse."
        )
    elif os.path.exists(source):
        print("Source " + source + " already exists; leaving the directory alone.")
    else:
        print("Destination " + dest + " not found; leaving the directory alone.")
PY

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

# Claude Code's per-project data, reversed the same way the Python undo does
# it: rename each entry directory back, then merge the saved copies of the
# files that were rewritten over what is there now. A manifest written before
# this feature has neither key and this block does nothing at all.
#
# The renames are walked in reverse so the reversal mirrors the order the
# migration applied them. Each one is checked rather than forced: a destination
# that is no longer a directory, or an old name that something has since taken,
# means the situation is not the one this backup describes, and guessing would
# risk clobbering whatever is there.
python3 - "$MANIFEST" "$BACKUP_DIR" <<'PY'
import json, os, shutil, sys

manifest_path, backup_dir = sys.argv[1], sys.argv[2]
with open(manifest_path) as handle:
    data = json.load(handle)

claude_root = data.get("claude_root") or os.path.join(data.get("home") or "", ".claude")

for old, new in reversed(data.get("claude_renames") or []):
    if os.path.isdir(new) and not os.path.exists(old):
        os.rename(new, old)
        print("Renamed Claude Code entry " + new + " back to " + old)
    elif os.path.exists(old):
        print("Claude Code entry " + old + " already exists; left it alone.")
    else:
        print("Claude Code entry " + new + " not found; left it alone.")

saved = os.path.join(backup_dir, "claude")
if os.path.isdir(saved):
    shutil.copytree(saved, claude_root, dirs_exist_ok=True)
    print("Restored Claude Code files from " + saved)

# The per-project registry is put back whole rather than merged, because it was
# rewritten whole. copy2 carries the saved permission bits across with the
# bytes, so a restore cannot widen who can read it. A run that renamed no
# registry keys recorded no path and saved no copy, and this does nothing.
registry = data.get("claude_registry")
saved_registry = os.path.join(backup_dir, "claude-registry.json")
if registry and os.path.isfile(saved_registry):
    shutil.copy2(saved_registry, registry)
    print("Restored Claude Code project registry " + registry)
PY

# The IDE's stored module definitions, reversed the same way the Python undo
# does it: rename each cache directory back to the name the source path hashes
# to, then rewrite the one absolute path recorded inside it the other way.
#
# The prefix matching is reimplemented here rather than imported, because this
# script has to work when the tool itself is what broke. It has to agree with
# the tool exactly, so the boundary set is the same one rewrite.py uses: a
# prefix matches only when the next character ends the path component, and
# space and ">" are deliberately not in that set because both are legal in a
# macOS directory name.
python3 - "$MANIFEST" <<'PY'
import json, os, re, sys

with open(sys.argv[1]) as handle:
    data = json.load(handle)

home = data.get("home") or ""
BOUNDARY = "(?=[/\"'<\r\n\t]|$)"


# Every spelling of the old prefix, paired with the new one.
def variants(old, new):
    pairs = [(old, new)]
    if home and old.startswith(home + "/"):
        macro_new = (
            "$USER_HOME$" + new[len(home):] if new.startswith(home + "/") else new
        )
        pairs.append(("$USER_HOME$" + old[len(home):], macro_new))
    for before, after in list(pairs):
        pairs.append(("file://" + before, "file://" + after))
    return pairs


# Replace every old prefix with its new one, in a single pass: one alternation,
# longest first, so no byte written by one variant can be matched again by
# another. Matching is case-insensitive because macOS is, and only the matched
# prefix is replaced - every byte after it is kept exactly as found.
def rewrite(text, pairs):
    mapping = {before.lower(): after for before, after in pairs}
    ordered = sorted(pairs, key=lambda pair: -len(pair[0]))
    pattern = re.compile(
        "(?:" + "|".join(re.escape(before) for before, _ in ordered) + ")" + BOUNDARY,
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: mapping[match.group(0).lower()], text)


# Rewrite the XML inside one cache directory. Binary neighbours are left alone.
def repair(directory, pairs):
    for root, _, names in os.walk(directory):
        for name in names:
            if not name.endswith(".xml"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8", newline="") as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            updated = rewrite(text, pairs)
            if updated != text:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(updated)


moves = data.get("moves") or [
    {
        "source": data.get("source"),
        "dest": data.get("dest"),
        "cache_renames": data.get("cache_renames") or [],
    }
]

for record in reversed(moves):
    source = str(record.get("source") or "")
    dest = str(record.get("dest") or "")
    # Destination first: undo rewrites the new path back to the old one.
    pairs = variants(dest, source)
    for old, new in reversed(record.get("cache_renames") or []):
        if os.path.isdir(new) and not os.path.exists(old):
            os.rename(new, old)
            repair(old, pairs)
            print("Renamed IDE cache " + new + " back to " + old)
        elif os.path.exists(old):
            print("IDE cache " + old + " already exists; left it alone.")
        else:
            print("IDE cache " + new + " not found; left it alone.")
PY

# Destination parent directories the run had to create because they did not
# exist. They come back out innermost first - the recorded order is outermost
# first, and a parent cannot be empty until its child has gone - and only while
# they are still empty, so anything a user has since put there survives. A
# manifest written before this key existed has none, and this does nothing.
python3 - "$MANIFEST" <<'PY'
import json, os, sys

with open(sys.argv[1]) as handle:
    data = json.load(handle)

for directory in reversed(data.get("created_dirs") or []):
    try:
        if os.path.isdir(directory) and not os.listdir(directory):
            os.rmdir(directory)
            print("Removed " + directory)
    except OSError:
        continue
PY

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
                    # symlinks=True, as in mover.py and undo.py: a link is
                    # copied as a link, so a restore puts back what was there
                    # rather than a copy of whatever it pointed at.
                    shutil.copytree(
                        origin,
                        config_root / product.name / subdir,
                        symlinks=True,
                    )
            names.append(product.name)
    except OSError as exc:
        raise BackupError(f"Could not create the backup: {exc}") from exc
    return names


def back_up_claude_files(
    claude_root: Path, files: Sequence[Path], backup_dir: Path
) -> list[str]:
    """Copy the Claude Code files a run is about to rewrite. Returns their paths.

    Only the files that will actually change are copied, and they are stored
    under ``<backup_dir>/claude/`` at their path relative to ``claude_root`` -
    at the location they have *before* the entry directories are renamed, which
    is where they still are when this runs.

    The renames themselves need no copy: an undo reverses one by renaming the
    directory back, so duplicating a transcript tree that can be hundreds of
    megabytes would buy nothing. ``shutil.copy2`` rather than ``copy`` keeps
    each file's permission bits, so a restore cannot widen who can read a
    transcript.

    Returns the relative paths copied, in the order given.
    """
    saved_root = backup_dir / "claude"
    copied: list[str] = []
    try:
        for path in files:
            relative = path.relative_to(claude_root)
            target = saved_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(relative.as_posix())
    except OSError as exc:
        raise BackupError(f"Could not create the backup: {exc}") from exc
    return copied


def back_up_registry(registry: Path, backup_dir: Path) -> Path:
    """Copy Claude Code's per-project registry into the backup. Returns the copy.

    The whole file is copied, not just the keys that are about to be renamed.
    Unlike an entry directory - whose rename an undo reverses by renaming back -
    the registry is rewritten in place, and it is one file holding every
    project's tool permissions and MCP servers, so the only reversal that can be
    trusted is putting the original bytes back.

    ``shutil.copy2`` rather than ``copy`` keeps the permission bits. This file
    routinely holds API-adjacent settings, so a restore must not be able to
    widen who can read it.
    """
    target = backup_dir / REGISTRY_BACKUP_NAME
    try:
        shutil.copy2(registry, target)
    except OSError as exc:
        raise BackupError(f"Could not create the backup: {exc}") from exc
    return target


def write_undo_script(backup_dir: Path) -> Path:
    """Write the standalone undo script into a backup directory."""
    script = backup_dir / UNDO_SCRIPT_NAME
    script.write_text(UNDO_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return script
