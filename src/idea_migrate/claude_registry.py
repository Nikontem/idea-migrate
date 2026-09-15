"""Repair Claude Code's project registry when a directory moves.

Claude Code keeps one JSON file, ``~/.claude.json``, as the registry of every
working directory it has ever been started in. Its top-level ``projects`` key
holds an object whose *keys* are absolute working-directory paths and whose
values are that project's settings - allowed tools, MCP servers, onboarding
flags. Move a project and the key still names the old location, so Claude Code
finds no settings for the new one and starts it as if it had never been seen
before: tool permissions, MCP servers and history-of-use all appear to have
vanished. Renaming the keys restores them.

Only the keys are touched. A settings object is opaque here - it may hold
anything Claude Code chooses to put in it, including strings that merely look
like paths - so the values are copied across untouched and never inspected.

The edit is a key rename, not a text substitution. Every other rewrite in this
tool edits raw text so that formatting survives, but a key cannot be renamed in
raw text without also matching the same path where it appears inside a value,
and a JSON object with two identical keys is not something a substitution can
notice it has created. Working on the parsed object makes both problems
impossible: a key either exists or it does not, and a collision is a fact about
a dictionary rather than a guess about a string.

Prefix matching over-reaches here for the same reason it does elsewhere. The key
``/x/foobar`` starts with ``/x/foo`` and ``/x/foo bar`` does too - a space is
legal in a macOS directory name - yet neither is inside the directory being
moved. A key therefore matches only when it equals the source or continues it
with a slash. Comparison is case-insensitive because macOS is: the same
directory entered once as ``IdeaProjects`` and once as ``ideaprojects`` leaves
two keys, both real, and both have to move. The key's own spelling of the tail
is kept - only the part standing for the source is replaced - so a trailing
slash or an unusual capitalisation comes through the rename intact.

Re-serializing the whole file is acceptable, which is not true of the JetBrains
XML or the session transcripts. Nothing but Claude Code reads this file, it
reads it with a JSON parser, and it writes it back with ``JSON.stringify`` at
two-space indentation with non-ASCII left unescaped - exactly what
:func:`json.dumps` produces with ``indent`` and ``ensure_ascii=False``. The
indentation actually found in the file is reused rather than assumed, and the
presence or absence of a trailing newline is preserved, so a run that changes
nothing meaningful also changes nothing cosmetic. The result is parsed back and
compared against the map that was expected before it is allowed to replace a
file that holds the user's entire tool-permission history.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ClaudeDataError, JsonIntegrityError
from .rewrite import read_preserving_newlines, write_text_atomically

logger = logging.getLogger(__name__)

REGISTRY_FILE_NAME = ".claude.json"
PROJECTS_KEY = "projects"

DEFAULT_INDENT = 2


@dataclass(frozen=True)
class KeyRename:
    """One ``projects`` key and the key it must become."""

    old: str
    new: str


@dataclass(frozen=True)
class RegistryPlan:
    """What the registry step will do, decided before anything is touched."""

    # The registry file the plan is about, kept so callers can name it in a
    # message without having to rebuild the path themselves.
    path: Path
    renames: list[KeyRename] = field(default_factory=list)


def _read(registry: Path) -> str | None:
    """Return the registry's text, or None if there is nothing to work with.

    None covers both "no such file" and "unusable", because the caller reacts to
    them the same way. The difference is only in the logging: a missing registry
    is the normal state on a machine that has never run Claude Code and says
    nothing, whereas a file that exists but cannot be read is reported so it is
    never mistaken for a registry with nothing to change.
    """
    if not registry.is_file():
        return None

    try:
        return read_preserving_newlines(registry)
    except UnicodeDecodeError:
        logger.warning("Skipped %s: it is not valid UTF-8 text.", registry)
        return None
    except OSError as exc:
        logger.warning("Skipped %s: it could not be read (%s).", registry, exc)
        return None


def _parse(
    registry: Path, text: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Parse registry text into the whole document and its ``projects`` object.

    Returns None, with a warning for anything genuinely malformed, when the text
    cannot yield a projects map. A registry that simply has no ``projects`` key
    is not malformed - it is what a fresh install looks like - so that case is
    silent.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        # Already broken before we arrived; not ours to fix or to blame.
        logger.warning(
            "Skipped %s: it was already not valid JSON before this run (%s).",
            registry,
            exc,
        )
        return None

    if not isinstance(data, dict):
        logger.warning(
            "Skipped %s: its top level is not a JSON object.", registry
        )
        return None

    if PROJECTS_KEY not in data:
        # A registry written before any project was opened. Nothing to do, and
        # nothing wrong either.
        return None

    projects = data[PROJECTS_KEY]
    if not isinstance(projects, dict):
        logger.warning(
            "Skipped %s: its %r value is not a JSON object.", registry, PROJECTS_KEY
        )
        return None

    return data, projects


def _renamed_key(key: str, source_posix: str, dest_posix: str) -> str | None:
    """Return the key's new spelling, or None if the move does not affect it.

    The key must be the source itself or continue it with a slash. Requiring the
    slash is what keeps ``/x/foobar`` and ``/x/foo bar`` out of a move of
    ``/x/foo``. Only the leading ``len(source)`` characters are replaced, so the
    key keeps its own tail character for character - including a trailing slash.

    The tail is taken from the key at the source's own length and the case-blind
    comparison is made on that same slice, rather than on lower-cased whole
    strings. A handful of codepoints change length when lower-cased, and on those
    a comparison of lower-cased strings would agree about a prefix whose length
    in the original key is something else entirely, splicing the new path onto
    the wrong offset.
    """
    tail = key[len(source_posix) :]
    if tail and not tail.startswith("/"):
        return None
    if key[: len(source_posix)].lower() != source_posix.lower():
        return None
    return dest_posix + tail


def plan_registry_renames(registry: Path, source: Path, dest: Path) -> RegistryPlan:
    """Work out which registry keys the move requires renaming.

    Keys come back in the order they appear in the file, so a caller listing them
    shows the user the file's own order rather than an invented one.

    A missing registry, one with no ``projects`` key, or one with no matching
    keys all give an empty plan. A registry that cannot be read or parsed gives
    an empty plan too, with a warning - one unusable file must not abort a move
    that is otherwise fine.

    Raises :class:`ClaudeDataError` when a key would be renamed onto one that
    already exists, or when two keys would end up with the same name. Both are
    checked here, at plan time, so the run stops before the directory has moved
    and there is nothing to undo.
    """
    text = _read(registry)
    if text is None:
        return RegistryPlan(path=registry, renames=[])

    parsed = _parse(registry, text)
    if parsed is None:
        return RegistryPlan(path=registry, renames=[])
    _, projects = parsed

    source_posix = source.as_posix()
    dest_posix = dest.as_posix()

    renames: list[KeyRename] = []
    for key in projects:
        new_key = _renamed_key(key, source_posix, dest_posix)
        if new_key is not None:
            renames.append(KeyRename(old=key, new=new_key))

    if not renames:
        return RegistryPlan(path=registry, renames=[])

    _check_collisions(registry, projects, renames)
    return RegistryPlan(path=registry, renames=renames)


def _check_collisions(
    registry: Path, projects: dict[str, Any], renames: Sequence[KeyRename]
) -> None:
    """Refuse a plan in which a rename would overwrite or duplicate a key.

    Comparison is case-insensitive, matching the rest of this module: two keys
    that differ only in case name one directory on this filesystem, so merging
    them would silently discard one project's settings.

    A key that is itself being renamed away is not an obstacle - it will not be
    there by the time the new name is used - which is what allows a move whose
    destination is already listed under its old name.
    """
    departing = {rename.old.lower() for rename in renames}
    staying = {key.lower() for key in projects} - departing
    claimed: dict[str, str] = {}

    for rename in renames:
        lowered = rename.new.lower()

        earlier = claimed.get(lowered)
        if earlier is not None:
            raise ClaudeDataError(
                f"Cannot rename the Claude Code registry keys {earlier!r} and "
                f"{rename.old!r} in {registry}: both would become {rename.new!r}."
            )
        claimed[lowered] = rename.old

        if lowered in staying:
            raise ClaudeDataError(
                f"Cannot rename the Claude Code registry key {rename.old!r} in "
                f"{registry} to {rename.new!r}: that key already exists."
            )


def _detect_indent(text: str) -> int:
    """Return the indentation width the file was written with.

    Claude Code writes two spaces, but a file that has been through another
    formatter may use four, and re-indenting the whole thing would turn a
    two-key rename into a diff covering every line. The first indented line
    decides; a file with no indented line at all - one written on a single line -
    gets the default.
    """
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return DEFAULT_INDENT


def _apply(
    registry: Path, projects: dict[str, Any], renames: Sequence[KeyRename]
) -> tuple[dict[str, Any], int]:
    """Return the projects map with the renames applied, and how many were made.

    Each key is renamed in place so the file's key order survives, and the value
    object is carried across by reference rather than rebuilt, so nothing inside
    a project's settings can be altered by this step.

    Renames are re-checked against the map as it is now rather than as it was
    when the plan was made: Claude Code may have been run in the meantime. A key
    that has since disappeared, or whose new name has since been taken, is
    skipped with a warning instead of being forced through.
    """
    current = projects
    applied = 0

    for rename in renames:
        if rename.old not in current:
            logger.warning(
                "Skipped renaming %s in %s: it is no longer listed.",
                rename.old,
                registry,
            )
            continue
        if rename.new in current:
            logger.warning(
                "Skipped renaming %s in %s: %s is already listed.",
                rename.old,
                registry,
                rename.new,
            )
            continue
        current = {
            (rename.new if key == rename.old else key): value
            for key, value in current.items()
        }
        applied += 1

    return current, applied


def rewrite_registry(
    registry: Path, renames: Sequence[KeyRename], dry_run: bool = False
) -> int:
    """Apply the planned key renames to the registry. Returns the number made.

    The file is read and parsed again rather than reusing what the plan saw,
    because Claude Code may have written to it in between - it does so on every
    session - and a stale copy would throw those changes away.

    Returns 0 without writing when nothing applied, or when the file has become
    unreadable or unparseable; those are logged so a skipped registry cannot be
    mistaken for a clean one. Raises :class:`JsonIntegrityError` if the text
    about to be written does not parse back into the map that was expected,
    leaving the original in place.
    """
    if not renames:
        return 0

    original = _read(registry)
    if original is None:
        return 0

    parsed = _parse(registry, original)
    if parsed is None:
        return 0
    data, projects = parsed

    updated, applied = _apply(registry, projects, renames)
    if applied == 0:
        return 0

    data[PROJECTS_KEY] = updated
    text = json.dumps(data, indent=_detect_indent(original), ensure_ascii=False)
    if original.endswith("\n"):
        text += "\n"

    _verify(registry, text, updated)

    if dry_run:
        return applied

    write_text_atomically(registry, text)
    return applied


def _verify(registry: Path, text: str, expected: dict[str, Any]) -> None:
    """Confirm the serialized text still holds exactly the map we built.

    This file records which tools the user has approved for which project, so
    the cost of writing a subtly wrong one is high and the cost of parsing a few
    hundred kilobytes twice is not.
    """
    try:
        reparsed = json.loads(text)
    except ValueError as exc:
        raise JsonIntegrityError(
            f"Rewriting {registry} would have produced invalid JSON: {exc}"
        ) from exc

    if not isinstance(reparsed, dict) or reparsed.get(PROJECTS_KEY) != expected:
        raise JsonIntegrityError(
            f"Rewriting {registry} would have changed its {PROJECTS_KEY} entries "
            f"in a way that was not planned; the file was left untouched."
        )
