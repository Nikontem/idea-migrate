"""Command-line interface.

Three shapes are supported:

    idea-migrate --source PATH --dest PATH
    idea-migrate backups
    idea-migrate undo <backup-dir>

This module is a thin layer over :mod:`idea_migrate.batch`. Every decision
about what a move implies, every guard that can refuse it, and every step that
actually changes something lives there; what is left here is reading the
arguments, asking the questions, printing the report and turning an error into
an exit code. The split is what lets the same decisions be reused by a caller
that is not a terminal - a batch of five moves, or another program - without
that caller inheriting a layer that prints as it goes.

Errors the tool raises deliberately are printed as a single line. A traceback
means a bug, not a user mistake.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import __version__
from .backup import UNDO_SCRIPT_NAME
from .batch import BatchRun, Operations, plan_moves, undo_run
from .claude_projects import preview_rewrites
from .claude_registry import rewrite_registry
from .completion import prompt_for_path
from .config import load_config
from .errors import MigrateError, RunFailed
from .listing import find_backups, format_backups
from .report import (
    format_claude_dry_run_sections,
    format_claude_plan,
    format_plan,
    format_result,
)
from .rewrite import rewrite_products


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


def _progress(message: str) -> None:
    """Announce a phase that takes a while, on stderr.

    A migration can move tens of gigabytes, back up a dozen products and walk
    a large project tree, and until now it did all of that in silence. Silence
    immediately after the riskiest step is exactly when a user reaches for
    Ctrl-C, so each long phase says what it is starting.

    These go to stderr so the report on stdout stays clean for anyone piping
    or redirecting it. There is deliberately no progress bar: the work is not
    divided into countable units of predictable size, so a bar would be a
    guess dressed up as a measurement.
    """
    print(message, file=sys.stderr)


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
        "To restore the previous state, run either of these:",
        f"  idea-migrate undo {backup_dir}",
        f"  {backup_dir / UNDO_SCRIPT_NAME}",
    ]:
        print(line, file=sys.stderr)


def _operations() -> Operations:
    """Bundle the four operations a run performs, as this module sees them.

    The functions are read out of this module's own namespace rather than left
    to their defaults in :class:`~idea_migrate.batch.Operations`, because the
    command-line tests replace ``idea_migrate.cli.rewrite_products``,
    ``idea_migrate.cli.preview_rewrites`` and ``idea_migrate.cli.rewrite_registry``
    in order to make a run fail at a chosen point. A default would bind the
    original function and the replacement would never run, so the names are
    looked up here, while the command is executing, and handed to the batch
    layer explicitly. ``move_directory`` is not replaced by any test and keeps
    its default.
    """
    return Operations(
        rewrite_products=rewrite_products,
        preview_rewrites=preview_rewrites,
        rewrite_registry=rewrite_registry,
    )


def run_migration(
    args: argparse.Namespace,
    home: Path,
    now: datetime,
    ps_output: str | None = None,
) -> int:
    """Run a migration. Returns a process exit code.

    The one move asked for on the command line is planned and executed as a
    batch of one, so the terminal and any other caller of
    :mod:`idea_migrate.batch` get the same guards in the same order.
    """
    config = load_config(Path(args.config) if args.config else None, home)

    source = args.source or prompt_for_path("Source directory: ")
    dest = args.dest or prompt_for_path("Destination directory: ")

    # Built once and used for both the planning and the run, so the dry runs
    # and the real rewrites go through the same four functions.
    operations = _operations()

    # Everything that can refuse the move is decided here, before a single line
    # of the plan is printed and while every directory is still in place: the
    # path checks, the running-IDE guard, and the dry runs that prove the XML,
    # the transcripts and the registry would all survive being rewritten.
    # PlanError carries one line per problem, and a batch of one has one
    # problem, so the message is exactly the single line this used to print.
    try:
        plan = plan_moves(
            [(source, dest)],
            config,
            home,
            ps_output=ps_output,
            operations=operations,
        )
    except MigrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    move = plan.moves[0]
    print(
        format_plan(
            move.source,
            move.dest,
            move.reference_count,
            len(plan.products),
            config.backup_root,
            cache_count=len(move.cache_renames),
        )
    )
    print(
        format_claude_plan(
            move.claude_renames,
            move.claude_rewrites,
            move.claude_unmatched,
            config.claude_root,
            registry_renames=move.registry_renames,
            registry_path=plan.registry_path,
        )
    )

    if args.dry_run:
        changed = move.config_changes
        if changed:
            print("Configuration files that would change:")
            for file_path, count in sorted(changed.items()):
                label = "reference" if count == 1 else "references"
                print(f"  {file_path}  ({count} {label})")
            print("")
        sections = format_claude_dry_run_sections(
            move.claude_renames,
            move.claude_rewrites,
            move.claude_unmatched,
            registry_renames=move.registry_renames,
            registry_path=plan.registry_path,
        )
        if sections:
            # Already ends in a blank line, so print it as-is.
            print(sections, end="")
        # The registry clause is appended only when there is one, so a machine
        # with no registry - or one nothing in it refers to - gets exactly the
        # sentence it always got rather than a trailing "and 0 keys". When it
        # is present the earlier "and" becomes a comma, so the sentence has one
        # conjunction and not two.
        if move.registry_renames:
            summary = (
                f"Dry run: {len(changed)} files would be modified, "
                f"{len(move.claude_renames)} Claude Code entries renamed, "
                f"{len(move.claude_rewrites)} Claude Code files rewritten and "
                f"{len(move.registry_renames)} project registry keys renamed"
            )
        else:
            summary = (
                f"Dry run: {len(changed)} files would be modified, "
                f"{len(move.claude_renames)} Claude Code entries renamed and "
                f"{len(move.claude_rewrites)} Claude Code files rewritten"
            )
        print(f"{summary}. Nothing was written.")
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

    run = BatchRun(plan, now=now, progress=_progress, operations=operations)
    try:
        result = run.execute()
    except RunFailed as exc:
        # The message is the underlying failure's own, unchanged. The recovery
        # advice is added only when a directory is somewhere other than where
        # it started, which the run read from the filesystem rather than
        # inferred from the kind of error.
        print(f"error: {exc}", file=sys.stderr)
        if run.state.anything_moved and run.state.backup_dir is not None:
            _print_recovery(run.state.backup_dir)
        return 1
    except BaseException:
        # Ctrl-C, or a bug we did not anticipate. The user still needs to
        # know a backup exists and how to reverse a move that already
        # happened, so print the recovery route before letting this
        # propagate to main() (or, for a bug, all the way out as a
        # traceback - that is still the right outcome for a bug).
        if run.state.anything_moved and run.state.backup_dir is not None:
            _print_recovery(run.state.backup_dir)
        raise

    done = result.moves[0]
    print(
        format_result(
            done.plan.source,
            done.plan.dest,
            done.rewritten_files,
            done.remaining,
            done.hardcoded_paths,
            result.backup_dir,
            claude_renames=done.plan.claude_renames,
            claude_rewritten=done.claude_rewritten,
            registry_renamed=done.registry_renamed,
            registry_path=plan.registry_path,
            cache_renamed=done.cache_renamed,
            cache_rewritten=done.cache_rewritten,
            relink_required=done.plan.cache_relink_required,
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
            # undo_run decides which settings directory to restore into and
            # reverses every move the run recorded, in reverse order, whether
            # that run moved one directory or several.
            result = undo_run(Path(args.backup_dir), config, now)
            for line in result.actions:
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
