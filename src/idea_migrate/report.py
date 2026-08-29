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

from collections.abc import Sequence
from pathlib import Path


def format_plan(
    source: Path,
    dest: Path,
    reference_count: int,
    product_count: int,
    backup_root: Path,
) -> str:
    """Describe the migration that is about to run."""
    return "\n".join(
        [
            "Planned migration",
            "",
            f"  Move:    {source}",
            f"      ->   {dest}",
            "",
            f"  Found {reference_count} stored path references across "
            f"{product_count} installed JetBrains products.",
            f"  A full backup of their settings will be written under {backup_root}",
            "",
        ]
    )


def format_result(
    source: Path,
    dest: Path,
    rewritten: dict[str, int],
    remaining: int,
    hardcoded_warnings: Sequence[str],
    backup_dir: Path,
) -> str:
    """Describe a completed migration, ending with the undo command."""
    total = sum(rewritten.values())
    lines = [
        "Migration complete",
        "",
        f"  Moved:   {source}",
        f"      ->   {dest}",
        f"  Repaired {total} path references in {len(rewritten)} files.",
    ]

    if remaining:
        lines.append(
            f"  WARNING: {remaining} references to the old path still remain. "
            "Inspect them before opening the IDEs."
        )

    if hardcoded_warnings:
        lines.extend(["", "  These project files contain hardcoded absolute paths"])
        lines.append("  and may need editing by hand:")
        lines.extend(f"    {path}" for path in hardcoded_warnings)

    lines.extend(
        [
            "",
            "  Each IDE will re-index the moved projects the first time it starts,",
            "  because its caches are keyed by path. This is slow but self-healing.",
            "",
            "-" * 72,
            f"Backup saved to: {backup_dir}",
            "This backup is never deleted automatically. To reverse this "
            "migration, run either of these:",
            f"  idea-migrate undo {backup_dir}",
            f"  {backup_dir / 'undo.sh'}",
        ]
    )
    return "\n".join(lines) + "\n"
