"""Command-line interface.

Three shapes are supported:

    idea-migrate --source PATH --dest PATH
    idea-migrate backups
    idea-migrate undo <backup-dir>

Errors the tool raises deliberately are printed as a single line. A traceback
means a bug, not a user mistake.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import __version__
from .backup import back_up_products, new_backup_dir, write_undo_script
from .completion import prompt_for_path
from .config import load_config
from .errors import MigrateError
from .listing import find_backups, format_backups
from .manifest import Manifest, write_manifest
from .mover import assert_no_ide_running, move_directory
from .paths import validate_move
from .products import find_product_dirs
from .report import format_plan, format_result
from .rewrite import count_references, prefix_variants, rewrite_products
from .undo import undo_backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idea-migrate",
        description=(
            "Move a JetBrains project directory and repair the stored path "
            "references in every installed IDE."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--source", help="Directory to move. Prompted for if omitted.")
    parser.add_argument("--dest", help="Where it should go. Prompted for if omitted.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and write nothing.",
    )
    parser.add_argument("--config", help="Path to a TOML configuration file.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("backups", help="List every backup and its undo command.")
    undo_parser = sub.add_parser("undo", help="Roll back a previous migration.")
    undo_parser.add_argument("backup_dir", help="The backup directory to roll back.")
    return parser


def _find_hardcoded_paths(root: Path, home: Path) -> list[str]:
    """Return XML files under a ``.idea`` directory in ``root`` whose text
    contains the literal ``home`` path.

    This only flags references to the user's home directory specifically -
    an absolute path elsewhere (such as ``/opt/sdk``) is not reported, and a
    reference that happens to be spelled relative to home is. It is a best
    effort scan run after a successful migration, so any error partway
    through returns whatever was found so far rather than failing the run.
    """
    warnings: list[str] = []
    needle = str(home)
    try:
        for idea_dir in root.rglob(".idea"):
            if not idea_dir.is_dir():
                continue
            for xml_file in idea_dir.rglob("*.xml"):
                try:
                    if needle in xml_file.read_text(encoding="utf-8"):
                        warnings.append(str(xml_file))
                except (OSError, UnicodeDecodeError):
                    continue
    except OSError:
        pass
    return sorted(warnings)


def _print_recovery(backup_dir: Path) -> None:
    """Print how to recover when a migration failed after the move started.

    This is driven by filesystem state (has the directory actually moved),
    not by the kind of exception that interrupted the run - a Ctrl-C or an
    unanticipated bug loses the directory just as surely as a MigrateError
    does, and the user needs the same recovery route either way.
    """
    for line in [
        "",
        "The migration failed partway through. A backup was taken before any "
        "change was made, and the directory may already have been moved.",
        f"To restore the previous state, run: {backup_dir / 'undo.sh'}",
    ]:
        print(line, file=sys.stderr)


def run_migration(
    args: argparse.Namespace,
    home: Path,
    now: datetime,
    ps_output: str | None = None,
) -> int:
    """Run a migration. Returns a process exit code."""
    config = load_config(Path(args.config) if args.config else None, home)

    source = args.source or prompt_for_path("Source directory: ")
    dest = args.dest or prompt_for_path("Destination directory: ")

    try:
        spec = validate_move(source, dest, home)
        assert_no_ide_running(ps_output)
    except MigrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    products = find_product_dirs(config.jetbrains_root, config.exclude_products)
    variants = prefix_variants(spec.source, spec.dest, spec.home)
    reference_count = count_references(products, variants)

    print(format_plan(spec.source, spec.dest, reference_count, len(products), config.backup_root))

    if args.dry_run:
        changed = rewrite_products(products, variants, dry_run=True)
        print(f"Dry run: {len(changed)} files would be modified. Nothing was written.")
        return 0

    if not args.yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            print("Cancelled. Nothing was changed.")
            return 0
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 0

    moved = False
    backup_dir: Path | None = None
    try:
        backup_dir = new_backup_dir(config.backup_root, now)
        backed_up = back_up_products(products, backup_dir)
        write_undo_script(backup_dir)

        manifest = Manifest(
            version=1,
            tool_version=__version__,
            created_at=now.isoformat(),
            home=str(spec.home),
            source=str(spec.source),
            dest=str(spec.dest),
            move_status="pending",
            backed_up_products=backed_up,
            rewritten_files={},
            undone_at=None,
        )
        write_manifest(backup_dir, manifest)

        move_directory(spec)
        moved = True
        rewritten = rewrite_products(products, variants)
        remaining = count_references(products, variants)

        write_manifest(backup_dir, replace(manifest, move_status="moved", rewritten_files=rewritten))
    except (MigrateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if moved and backup_dir is not None:
            _print_recovery(backup_dir)
        return 1
    except BaseException:
        # Ctrl-C, or a bug we did not anticipate. The user still needs to
        # know a backup exists and how to reverse a move that already
        # happened, so print the recovery route before letting this
        # propagate to main() (or, for a bug, all the way out as a
        # traceback - that is still the right outcome for a bug).
        if moved and backup_dir is not None:
            _print_recovery(backup_dir)
        raise

    warnings = _find_hardcoded_paths(spec.dest, spec.home)
    print(
        format_result(
            spec.source, spec.dest, rewritten, remaining, warnings, backup_dir
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="warning: %(message)s")

    args = build_parser().parse_args(argv)

    if args.dry_run and args.command in ("backups", "undo"):
        print(
            "error: --dry-run has no effect on the "
            f"'{args.command}' subcommand and is not supported with it.",
            file=sys.stderr,
        )
        return 1

    home = Path.home()
    now = datetime.now()

    try:
        if args.command == "backups":
            config = load_config(Path(args.config) if args.config else None, home)
            print(format_backups(find_backups(config.backup_root), config.backup_root))
            return 0
        if args.command == "undo":
            config = load_config(Path(args.config) if args.config else None, home)
            for line in undo_backup(Path(args.backup_dir), config.jetbrains_root, now):
                print(line)
            return 0
        return run_migration(args, home, now)
    except (MigrateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
