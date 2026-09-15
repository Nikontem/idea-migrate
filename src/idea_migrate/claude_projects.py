"""Move Claude Code's per-project data alongside the directory being migrated.

Claude Code keeps one directory per working directory under
``~/.claude/projects/``. The directory's name is the absolute path of that
working directory with every character outside ``[A-Za-z0-9]`` replaced by a
dash, so ``/Users/nikos/IdeaProjects/api`` is stored as
``-Users-nikos-IdeaProjects-api``. Moving a project therefore orphans its
transcripts: Claude Code looks under the new encoded name, finds nothing, and
the session history silently disappears. Renaming the entry directories brings
it back.

Three things make that harder than a string substitution.

The encoding is lossy. A dash in the encoded name may stand for ``/``, ``.``,
``_``, a space, or a literal dash, so a name cannot be decoded back into a path.
The only sound direction is forward: encode real directories found on disk and
compare. That is why :func:`plan_entry_renames` walks the filesystem rather than
parsing entry names, and why an entry with no matching directory is reported as
unmatched instead of guessed at.

Prefix matching over-reaches. The entry for ``/x/foobar`` starts with the
encoding of ``/x/foo``, and the entry for the sibling directory ``/x/foo-bar``
is spelled exactly like one for ``/x/foo/bar``. A candidate must therefore match
the source's encoding exactly or continue with a dash, and even then it only
becomes a rename once a real directory encodes to it.

The walk must stay cheap. A project can contain a ``node_modules`` tree with
tens of thousands of directories and no Claude data at all. The walk descends
into a directory only while some entry it has not yet matched lies below it, so
those trees are never entered.

Rewriting the transcripts is the second half of the job. The files are
line-delimited JSON, so a bad substitution corrupts a record rather than merely
mis-naming a path; every line is parsed before and after the rewrite, and a
change that would produce invalid JSON aborts with the file untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ClaudeDataError, JsonIntegrityError
from .rewrite import (
    JSON_BOUNDARY,
    read_preserving_newlines,
    rewrite_text,
    write_text_atomically,
)

logger = logging.getLogger(__name__)

PROJECTS_DIR_NAME = "projects"
HISTORY_FILE_NAME = "history.jsonl"

_NON_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]")


def encode_project_path(path: Path) -> str:
    """Return the ``~/.claude/projects`` directory name for a working directory.

    Every character that is not a letter or a digit becomes a dash - separators,
    dots, underscores and spaces alike. The transformation is one character in,
    one character out, which is what lets :func:`plan_entry_renames` splice a new
    prefix onto an existing name by slicing at a fixed offset.
    """
    return _NON_ALPHANUMERIC.sub("-", path.as_posix())


@dataclass(frozen=True)
class EntryRename:
    """One ``~/.claude/projects`` directory and the name it must take."""

    old: Path
    new: Path
    # The real directory the entry stands for, at its pre-move location. Kept so
    # callers can explain a rename in terms the user recognises.
    project: Path


@dataclass(frozen=True)
class ClaudePlan:
    """What the rename step will do, decided before anything is touched."""

    renames: list[EntryRename] = field(default_factory=list)
    # Entries whose name looks like it belongs to the source but that no real
    # directory under the source encodes to: a sibling such as /x/foo-bar next to
    # /x/foo, or a leftover from a subdirectory that has since been deleted.
    # Renaming those would be a guess, so they are listed and left alone.
    unmatched: list[Path] = field(default_factory=list)


def _candidate_entries(projects_dir: Path, source_encoded: str) -> dict[str, str]:
    """Return entry names that could belong to the source, keyed by lower case.

    A name qualifies when it is the source's own encoding or continues it with a
    dash. Requiring the dash is what stops the entry for ``/x/foobar`` from being
    dragged along by a move of ``/x/foo``.

    Comparison is case-insensitive because macOS is: the same directory opened as
    ``IdeaProjects`` once and ``ideaprojects`` another time yields two spellings
    of one entry name, and the one on disk is whatever Claude Code was given.
    """
    prefix = source_encoded.lower()
    with_dash = prefix + "-"
    candidates: dict[str, str] = {}
    try:
        with os.scandir(projects_dir) as scan:
            entries = list(scan)
    except OSError as exc:
        logger.warning("Skipped %s: it could not be listed (%s).", projects_dir, exc)
        return candidates
    for entry in entries:
        lowered = entry.name.lower()
        if lowered != prefix and not lowered.startswith(with_dash):
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        candidates[lowered] = entry.name
    return candidates


def plan_entry_renames(claude_root: Path, source: Path, dest: Path) -> ClaudePlan:
    """Work out which project entries the move requires renaming.

    Candidate entry names are collected first, then the real filesystem is walked
    downwards from ``source``: each directory is encoded and looked up among the
    candidates still waiting for a match. Going in this direction is the only
    sound one, because the encoding cannot be reversed.

    Directories only, and symlinks are never followed - a symlinked directory is
    not where Claude Code recorded its cwd, and following one could walk out of
    the source entirely or loop. Unreadable directories are stepped over rather
    than raised on: one directory the user cannot list should not abandon the
    other entries.

    A missing ``projects`` directory means Claude Code has no data here and the
    plan is empty. Raises :class:`ClaudeDataError` if a destination name is
    already taken, which would otherwise make ``os.rename`` clobber it.
    """
    projects_dir = claude_root / PROJECTS_DIR_NAME
    if not projects_dir.is_dir():
        return ClaudePlan(renames=[], unmatched=[])

    source_encoded = encode_project_path(source)
    remaining = _candidate_entries(projects_dir, source_encoded)
    if not remaining:
        return ClaudePlan(renames=[], unmatched=[])

    dest_encoded = encode_project_path(dest)
    renames: list[EntryRename] = []

    pending = [source]
    while pending:
        directory = pending.pop()
        encoded = encode_project_path(directory)
        name = remaining.pop(encoded.lower(), None)
        if name is not None:
            # The encoding maps one character to one character, so the tail after
            # the source's own prefix is the same length in the entry name as in
            # the encoded source. Slicing rather than re-encoding the directory
            # keeps whatever spelling the entry already used.
            renames.append(
                EntryRename(
                    old=projects_dir / name,
                    new=projects_dir / (dest_encoded + name[len(source_encoded) :]),
                    project=directory,
                )
            )
        # Descend only while an entry we have not matched yet could be below
        # here. Without this a node_modules tree would be walked in full for
        # nothing.
        below = (encoded + "-").lower()
        if not any(key.startswith(below) for key in remaining):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            # Unreadable or vanished mid-walk; the entries below it simply stay
            # unmatched and are reported as such.
            continue

    for rename in renames:
        if rename.new.exists() or rename.new.is_symlink():
            raise ClaudeDataError(
                f"Cannot rename the Claude Code project entry {rename.old} to "
                f"{rename.new}: that name already exists."
            )

    return ClaudePlan(
        renames=sorted(renames, key=lambda item: item.old.name),
        unmatched=sorted(projects_dir / name for name in remaining.values()),
    )


def json_variants(source: Path, dest: Path) -> list[tuple[str, str]]:
    """Return the one prefix pair to search for inside Claude Code's JSON.

    Unlike JetBrains configuration, these files record plain absolute paths only
    - no ``$USER_HOME$`` placeholder and no ``file://`` URLs - so a single pair
    covers everything.
    """
    return [(source.as_posix(), dest.as_posix())]


def rewritable_files(
    claude_root: Path, renames: Sequence[EntryRename]
) -> list[Path]:
    """List the JSON-lines files that may mention the old location.

    That is the shared command history plus every transcript under an entry being
    renamed, including the nested ``<session>/subagents/*.jsonl`` ones. Paths are
    returned at their pre-rename location, because this list is used to take the
    backup before anything moves.
    """
    found: list[Path] = []
    history = claude_root / HISTORY_FILE_NAME
    if history.is_file():
        found.append(history)
    for rename in renames:
        found.extend(
            path for path in sorted(rename.old.rglob("*.jsonl")) if path.is_file()
        )

    # Two renames cannot nest - every entry lives directly in projects/ - but
    # de-duplicating keeps a repeated rename or a re-listed history file from
    # being backed up and counted twice.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _json_records(text: str) -> list[str]:
    """Split JSON-lines text into its non-blank records.

    ``str.splitlines`` is deliberately not used: it also breaks on form feed,
    U+2028 and friends, all of which are legal unescaped characters inside a JSON
    string and would turn one valid record into two invalid ones. Only a line
    feed ends a record here, with a carriage return before it belonging to a CRLF
    ending rather than to the data.
    """
    records = []
    for raw in text.split("\n"):
        record = raw.rstrip("\r")
        if record.strip():
            records.append(record)
    return records


def rewrite_jsonl_file(
    path: Path, variants: Sequence[tuple[str, str]], dry_run: bool = False
) -> int:
    """Rewrite one JSON-lines file in place. Returns the replacements made.

    The mirror of :func:`rewrite.rewrite_file` for transcripts: the edit is made
    on raw text so that key order, spacing and escaping survive untouched, and
    the result is validated before it is written.

    Returns 0 without writing when nothing matched, when the file could not be
    read, or when it did not parse as JSON to begin with; the last three are
    logged as warnings so a skipped file cannot be mistaken for a clean one.
    Raises :class:`JsonIntegrityError` if the rewrite would produce a line that no
    longer parses - a destination path containing a double quote would do it -
    leaving the original in place.
    """
    try:
        original = read_preserving_newlines(path)
    except UnicodeDecodeError:
        logger.warning("Skipped %s: it is not valid UTF-8 text.", path)
        return 0
    except OSError as exc:
        logger.warning("Skipped %s: it could not be read (%s).", path, exc)
        return 0

    for record in _json_records(original):
        try:
            json.loads(record)
        except ValueError as exc:
            # Already broken before we arrived; not ours to fix or to blame.
            logger.warning(
                "Skipped %s: it was already not valid JSON before this run (%s).",
                path,
                exc,
            )
            return 0

    updated, count = rewrite_text(original, variants, boundary=JSON_BOUNDARY)
    if count == 0:
        return 0

    for record in _json_records(updated):
        try:
            json.loads(record)
        except ValueError as exc:
            raise JsonIntegrityError(
                f"Rewriting {path} would have produced invalid JSON: {exc}"
            ) from exc

    if dry_run:
        return count

    write_text_atomically(path, updated)
    return count


def preview_rewrites(
    files: Iterable[Path], variants: Sequence[tuple[str, str]]
) -> dict[str, int]:
    """Count the references each file holds, without writing anything.

    Returns a mapping of file path to replacement count, listing only the files
    that would actually change.
    """
    return _rewrite_all(files, variants, dry_run=True)


def rewrite_files(
    files: Iterable[Path], variants: Sequence[tuple[str, str]]
) -> dict[str, int]:
    """Rewrite each file, returning the per-file counts of the ones that changed."""
    return _rewrite_all(files, variants, dry_run=False)


def _rewrite_all(
    files: Iterable[Path], variants: Sequence[tuple[str, str]], dry_run: bool
) -> dict[str, int]:
    changed: dict[str, int] = {}
    for path in files:
        count = rewrite_jsonl_file(path, variants, dry_run=dry_run)
        if count:
            changed[str(path)] = count
    return changed


def rename_entries(renames: Sequence[EntryRename]) -> list[tuple[str, str]]:
    """Perform the planned renames. Returns the pairs actually carried out.

    No parent directories are created: every entry lives directly in the one flat
    ``projects`` directory, so the destination's parent is the source's. A source
    that has disappeared since the plan was made is skipped with a warning rather
    than failing the migration, and is left out of the returned list so that undo
    does not try to reverse a rename that never happened.
    """
    performed: list[tuple[str, str]] = []
    for rename in renames:
        if not rename.old.exists():
            logger.warning(
                "Skipped renaming %s: it no longer exists.", rename.old
            )
            continue
        os.rename(rename.old, rename.new)
        performed.append((str(rename.old), str(rename.new)))
    return performed


def relocate(path: Path, renames: Sequence[EntryRename]) -> Path:
    """Map a pre-rename file path to where it lives once the renames are done.

    A path under no rename - the shared history file, for one - comes back
    unchanged. The longest matching entry wins, so nesting would resolve to the
    most specific rename rather than an ancestor's.
    """
    best: tuple[EntryRename, Path] | None = None
    for rename in renames:
        try:
            relative = path.relative_to(rename.old)
        except ValueError:
            continue
        if best is None or len(str(rename.old)) > len(str(best[0].old)):
            best = (rename, relative)
    if best is None:
        return path
    return best[0].new / best[1]
