"""Format what the tool is about to do, and what it did.

The result report ends with the backup location and both undo routes for this
run - the ``idea-migrate undo`` subcommand and the standalone script in the
backup directory. That placement is deliberate: it is the thing the user needs
when something has gone wrong, so it must not be buried in the middle of a wall
of output. Both routes are named because they fail in different ways: the
subcommand needs the tool to still be installed and working, while the script
needs only bash and python3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .backup import UNDO_SCRIPT_NAME
from .claude_projects import EntryRename
from .claude_registry import KeyRename


def _reference_label(count: int) -> str:
    """Return "reference" or "references" to match a count."""
    return "reference" if count == 1 else "references"


def format_plan(
    source: Path,
    dest: Path,
    reference_count: int,
    product_count: int,
    backup_root: Path,
    cache_count: int = 0,
) -> str:
    """Describe the migration that is about to run.

    ``cache_count`` is keyword-optional so a caller that predates the IDE cache
    step still gets a correct plan; zero means the line is left out entirely,
    which is the ordinary case for a project that keeps its modules in .idea.
    """
    lines = [
        "Planned migration",
        "",
        f"  Move:    {source}",
        f"      ->   {dest}",
        "",
        f"  Found {reference_count} stored path references across "
        f"{product_count} installed JetBrains products.",
        f"  A full backup of their settings will be written under {backup_root}",
    ]
    if cache_count:
        store_word = "store" if cache_count == 1 else "stores"
        lines.append(
            f"  {cache_count} stored module {store_word} will move with the "
            f"project. These are renamed rather than copied, so undo reverses "
            f"them without a backup."
        )
    lines.append("")
    return "\n".join(lines)


def format_claude_plan(
    renames: Sequence[EntryRename],
    rewrites: Mapping[str, int],
    unmatched: Sequence[Path],
    claude_root: Path,
    *,
    registry_renames: Sequence[KeyRename] = (),
    registry_path: Path | None = None,
) -> str:
    """Summarise what the move means for Claude Code's stored project data.

    One line, printed directly under the migration plan in both a dry run and a
    real one. Zero is spelled out in words rather than printed as a number,
    because "0 entries to rename, 0 files to rewrite" reads like a failure when
    it is in fact the ordinary case for a directory Claude Code has never been
    run in.

    Entries that merely look related are counted separately: they are the one
    thing here the tool deliberately declines to touch, and a user who is
    expecting a transcript to follow the move needs to know it will not.

    The registry is a separate line, and only when it has keys to rename. It is
    a different file in a different place holding a different kind of thing -
    tool permissions and MCP servers rather than session transcripts - so
    folding its count into the sentence above would say that two unrelated
    numbers were about the same thing. A registry with nothing to rename adds no
    line at all, including when there is other Claude Code work to report: the
    "no Claude Code project data" sentence is about the entries and stays as it
    is, and a run with entries to move has nothing to say about a registry it is
    leaving alone.
    """
    if not renames and not rewrites:
        lines = ["  No Claude Code project data refers to the old location."]
    else:
        lines = [
            f"  Claude Code project data: {len(renames)} entries to rename, "
            f"{len(rewrites)} files to rewrite (under {claude_root})."
        ]
    if registry_renames:
        key_word = "key" if len(registry_renames) == 1 else "keys"
        lines.append(
            f"  Claude Code project registry: {len(registry_renames)} {key_word} "
            f"to rename in {registry_path}."
        )
    if unmatched:
        entry_word = "entry" if len(unmatched) == 1 else "entries"
        lines.append(
            f"  {len(unmatched)} similarly named {entry_word} will be left alone."
        )
    return "\n".join(lines + [""])


def format_claude_dry_run_sections(
    renames: Sequence[EntryRename],
    rewrites: Mapping[str, int],
    unmatched: Sequence[Path],
    *,
    registry_renames: Sequence[KeyRename] = (),
    registry_path: Path | None = None,
) -> str:
    """Spell out, for a dry run, every entry and file the Claude step would touch.

    Four sections, each omitted when it is empty, so a project with no Claude
    Code history prints nothing at all rather than four empty headings. Returns
    text already ending in a blank line, or "" when there is nothing to say.

    The registry keys are shown in full, old above new, rather than counted. The
    keys are absolute paths and a rename is only ever a prefix substitution, so
    seeing the pairs is how a user confirms that the tool matched the
    directories it was meant to and not a sibling with a similar name - which is
    the whole thing a dry run exists to let them check.
    """
    lines: list[str] = []

    if renames:
        lines.append("Claude Code project entries that would be renamed:")
        for rename in renames:
            lines.append(f"  {rename.old}")
            lines.append(f"    ->   {rename.new}")
        lines.append("")

    if rewrites:
        lines.append("Claude Code files that would be rewritten:")
        for file_path, count in sorted(rewrites.items()):
            lines.append(f"  {file_path}  ({count} {_reference_label(count)})")
        lines.append("")

    if registry_renames:
        lines.append(
            f"Claude Code project registry keys that would be renamed "
            f"(in {registry_path}):"
        )
        for rename in registry_renames:
            lines.append(f"  {rename.old}")
            lines.append(f"    ->   {rename.new}")
        lines.append("")

    if unmatched:
        lines.append(
            "Claude Code entries left alone (similar name, but no matching "
            "directory under the source):"
        )
        lines.extend(f"  {path}" for path in unmatched)
        lines.append("")

    if not lines:
        return ""
    # The last element is an empty string, so the join ends the final entry's
    # line; the extra newline is the blank line that separates the block from
    # whatever the caller prints next.
    return "\n".join(lines) + "\n"


def format_result(
    source: Path,
    dest: Path,
    rewritten: dict[str, int],
    remaining: int,
    hardcoded_warnings: Sequence[str],
    backup_dir: Path,
    *,
    claude_renames: Sequence[EntryRename] = (),
    claude_rewritten: Mapping[str, int] | None = None,
    registry_renamed: int = 0,
    registry_path: Path | None = None,
    cache_renamed: Sequence[tuple[str, str]] = (),
    cache_rewritten: Mapping[str, int] | None = None,
    relink_required: bool = False,
) -> str:
    """Describe a completed migration, ending with the undo command.

    The Claude Code arguments are keyword-only and default to empty, so a caller
    that has nothing to say about Claude Code - or predates the feature - still
    gets a correct report.

    The registry gets a line only when keys were actually renamed. It is not
    given the "nothing to do" sentence the other two counts get, because a
    machine with no ``~/.claude.json`` at all is ordinary and reporting on a
    file that does not exist would raise a question rather than answer one.
    """
    total = sum(rewritten.values())
    lines = [
        "Migration complete",
        "",
        f"  Moved:   {source}",
        f"      ->   {dest}",
    ]

    if rewritten:
        lines.append(
            f"  Repaired {total} path references in {len(rewritten)} files."
        )
    else:
        # "Repaired 0 references in 0 files" reads like a failure. Say which
        # of the two it was: the search ran and found nothing to change.
        lines.append(
            "  No path references needed repairing: no configuration file "
            "mentioned the old location."
        )

    claude_files = dict(claude_rewritten or {})
    if claude_renames or claude_files:
        lines.append(
            f"  Renamed {len(claude_renames)} Claude Code project entries and "
            f"repaired {sum(claude_files.values())} references in "
            f"{len(claude_files)} of their files."
        )
    else:
        # Same reasoning as above: a row of zeroes looks like a malfunction,
        # so say that the search ran and came back empty.
        lines.append("  No Claude Code project data referred to the old location.")

    if registry_renamed:
        # "keys" regardless of the count, matching the line above it: the
        # completed-migration report reads as a tally, and "1 entries" already
        # sets that register. The plan, which reads as a sentence about what is
        # about to happen, does inflect.
        lines.append(
            f"  Renamed {registry_renamed} keys in the Claude Code project "
            f"registry ({registry_path})."
        )

    cache_files = dict(cache_rewritten or {})
    if cache_renamed:
        store_word = "store" if len(cache_renamed) == 1 else "stores"
        lines.append(
            f"  Moved {len(cache_renamed)} stored module {store_word} and "
            f"repaired {sum(cache_files.values())} references in "
            f"{len(cache_files)} of their files."
        )

    if remaining:
        lines.append(
            f"  WARNING: {remaining} references to the old path still remain. "
            "Inspect them before opening the IDEs."
        )

    if hardcoded_warnings:
        lines.extend(["", "  These project files contain hardcoded absolute paths"])
        lines.append("  and may need editing by hand:")
        lines.extend(f"    {path}" for path in hardcoded_warnings)

    if relink_required:
        # The one cache that does not rebuild itself, and the symptom of it
        # missing looks exactly like a failed migration - so say what it is and
        # what fixes it, rather than leaving the user to guess.
        lines.extend(
            [
                "",
                "  This project keeps its module definitions outside its .idea",
                "  directory, and no stored copy could be matched to its old",
                "  location. The IDE will open it with no modules - an empty",
                "  Maven or Gradle tool window. To fix it, right-click the build",
                "  file (pom.xml or build.gradle) and choose to add it as a",
                "  project; nothing else is lost.",
            ]
        )

    lines.extend(
        [
            "",
            "  Each IDE will re-index the moved projects the first time it starts,",
            "  because its caches are keyed by path. Re-indexing is slow but",
            "  self-healing; the module definitions above are the one part that",
            "  is not, which is why they are moved rather than left to rebuild.",
            "",
            "-" * 72,
            f"Backup saved to: {backup_dir}",
            "This backup is never deleted automatically. To reverse this "
            "migration, run either of these:",
            f"  idea-migrate undo {backup_dir}",
            f"  {backup_dir / UNDO_SCRIPT_NAME}",
        ]
    )
    return "\n".join(lines) + "\n"
