"""Plan and run several moves as one reversible unit.

The single-move flow in ``cli.py`` interleaves three jobs: deciding what a move
implies, doing it, and telling the user about it. Moving a handful of projects
in one go needs the first two without the third - a caller that prints as it
goes cannot be asked to plan five moves and report every problem at once - so
they live here, behind an API that returns values and raises errors instead of
printing.

Two properties are the point of doing it this way rather than looping over the
single-move flow.

Everything is decided before anything is touched. :func:`plan_moves` runs every
preflight for every move - path validation, the running-IDE guard, the overlap
guard, and the dry runs that prove the XML, the transcripts and the registry
would survive being rewritten - and raises :class:`PlanError` carrying *every*
problem it found. A user fixing five destinations learns about all five now
rather than rediscovering the next one on each attempt.

One run leaves one backup. :class:`BatchRun` takes a single backup, writes a
single manifest recording one entry per move, and updates that manifest as each
move completes. Both undo routes - ``undo.py`` and the standalone ``undo.sh``
in every backup - already read that shape and reverse the moves in reverse
order, so a batch is undone by the same one command as a single move, whether
it finished or stopped half way through.

Nothing here prints. Progress is reported through a callback so the caller
decides where it goes, and a failure part-way through raises
:class:`RunFailed`, whose ``state`` says how far the run got: which moves
completed, which one was in progress, and whether that one's directory had
already left its source. That is what a caller needs in order to print recovery
advice, and it is filed as facts rather than as prose.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import __version__, claude_projects, claude_registry, mover, rewrite
from .backup import (
    back_up_claude_files,
    back_up_products,
    back_up_registry,
    new_backup_dir,
    write_undo_script,
)
from .claude_projects import (
    EntryRename,
    json_variants,
    plan_entry_renames,
    relocate,
    rename_entries,
    rewritable_files,
    rewrite_files,
)
from .claude_registry import KeyRename, plan_registry_renames
from .config import Config
from .errors import MigrateError, PlanError, RunFailed
from .ide_caches import (
    CacheRename,
    plan_cache_renames,
    rename_caches,
    rewrite_caches,
)
from .manifest import Manifest, read_manifest, write_manifest
from .mover import assert_no_ide_running
from .paths import MoveCheck, MoveSpec, check_move
from .products import find_product_dirs
from .rewrite import count_references, prefix_variants
from .undo import undo_backup

__all__ = [
    "BatchPlan",
    "BatchRun",
    "MovePlan",
    "MoveResult",
    "Operations",
    "RunResult",
    "RunState",
    "UndoResult",
    "find_hardcoded_paths",
    "plan_moves",
    "undo_run",
    "undo_settings_root",
]


@dataclass(frozen=True)
class MovePlan:
    """Everything one move implies, decided before anything is touched."""

    source: Path
    dest: Path
    same_device: bool
    # References to the old location currently stored in the IDE settings.
    reference_count: int
    # Configuration file -> references in it the rewrite would change, from the
    # dry run that also proves each rewritten file would still be valid XML.
    config_changes: dict[str, int]
    claude_renames: list[EntryRename]
    # Claude Code file -> references in it, listed at its PRE-rename path,
    # which is where the file still is when the backup is taken.
    claude_rewrites: dict[str, int]
    claude_unmatched: list[Path]
    registry_renames: list[KeyRename]
    # The IDE caches holding this project's module definitions, which live
    # outside the project and are keyed by its path. See ide_caches.
    cache_renames: list[CacheRename]
    # Cache directories whose name matched but whose contents never named the
    # source, so they are reported rather than renamed.
    cache_unconfirmed: list[Path]
    # True when the project keeps its modules outside .idea and no cache could
    # be matched, so the build system will need re-linking by hand after the
    # move. Worth saying, because the symptom - an empty Maven tool window -
    # looks like the move went wrong.
    cache_relink_required: bool


@dataclass(frozen=True)
class BatchPlan:
    """One plan covering every move in the batch."""

    home: Path
    config: Config
    products: list[Path]
    # In the order the caller gave them, which is the order they will be made.
    moves: list[MovePlan]
    # Destination parent directories that do not exist yet, de-duplicated and
    # outermost first. Empty unless the caller asked for them to be created.
    missing_parents: list[Path]
    registry_path: Path

    @property
    def total_references(self) -> int:
        """References to every old location, across every move."""
        return sum(move.reference_count for move in self.moves)


@dataclass(frozen=True)
class MoveResult:
    """What one move actually did."""

    plan: MovePlan
    rewritten_files: dict[str, int]
    # References to the old location still found after the rewrite. Zero is the
    # expected answer; anything else is worth showing the user.
    remaining: int
    claude_renamed: list[tuple[str, str]]
    # Post-rename file path -> references repaired in it.
    claude_rewritten: dict[str, int]
    registry_renamed: int
    cache_renamed: list[tuple[str, str]]
    # Post-rename file path -> references repaired in it.
    cache_rewritten: dict[str, int]
    hardcoded_paths: list[str]


@dataclass(frozen=True)
class RunResult:
    """What a completed run did, and where its backup is."""

    backup_dir: Path
    moves: list[MoveResult]
    created_dirs: list[Path]


@dataclass
class RunState:
    """How far a run has got. Updated as it goes, and read after a failure.

    This is the only thing a caller has to work with when a run stops part-way
    through, so it records facts rather than conclusions: what exists, what
    finished, and what the filesystem says about the move that was in progress.
    """

    backup_dir: Path | None = None
    completed: list[MoveResult] = field(default_factory=list)
    # The move that was in progress when the run stopped, if any.
    failed: MovePlan | None = None
    # Whether that move's directory had already left its source. Taken from the
    # filesystem rather than from the kind of error, because a Ctrl-C loses the
    # directory just as surely as a failed rewrite does.
    failed_moved: bool = False
    pending: list[MovePlan] = field(default_factory=list)

    @property
    def anything_moved(self) -> bool:
        """True when a directory is somewhere other than where it started."""
        return bool(self.completed) or self.failed_moved


@dataclass(frozen=True)
class Operations:
    """The four operations a run performs, injectable for tests.

    The command-line layer's tests provoke a partial failure by patching the
    names ``cli.py`` imported, so the caller passes the functions it holds
    rather than having this module reach for its own. The same seam lets any
    caller - including this module's own tests - fail one move of a batch on
    purpose and check what the run leaves behind.
    """

    move_directory: Callable[[MoveSpec], None] = field(
        default=mover.move_directory
    )
    rewrite_products: Callable[..., dict[str, int]] = field(
        default=rewrite.rewrite_products
    )
    preview_rewrites: Callable[..., dict[str, int]] = field(
        default=claude_projects.preview_rewrites
    )
    rewrite_registry: Callable[..., int] = field(
        default=claude_registry.rewrite_registry
    )


def find_hardcoded_paths(root: Path, home: Path) -> list[str]:
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


def _comparable(path: Path) -> str:
    """Return the spelling two paths are compared as when neither may exist.

    Lower case because the filesystem this tool is written for is
    case-insensitive, matching how the rest of the modules compare paths, and
    lexical because the overlap guard runs before anything has been created:
    ``os.path.samefile`` cannot answer a question about a directory that is not
    there yet.
    """
    return path.as_posix().lower()


def _at_or_inside(inner: Path, outer: Path) -> bool:
    """True when ``inner`` names ``outer`` itself or something below it."""
    left, right = _comparable(inner), _comparable(outer)
    return left == right or left.startswith(right.rstrip("/") + "/")


def _overlap_problem(first: MoveCheck, second: MoveCheck) -> str | None:
    """Return why two moves cannot both be made, or None when they can.

    Four ways two moves collide, each checked in both directions: two sources
    that contain one another (the outer move would carry the inner directory
    off with it), two destinations that do (one move would land inside the
    other's result), a destination inside the other's source, and a source
    inside the other's destination. Only the first collision found is reported,
    because a pair that overlaps in two ways is still one thing to fix.
    """
    for left_label, left, right_label, right in (
        ("source", first.source, "source", second.source),
        ("source", second.source, "source", first.source),
        ("destination", first.dest, "destination", second.dest),
        ("destination", second.dest, "destination", first.dest),
        ("destination", first.dest, "source", second.source),
        ("destination", second.dest, "source", first.source),
        ("source", first.source, "destination", second.dest),
        ("source", second.source, "destination", first.dest),
    ):
        if not _at_or_inside(left, right):
            continue
        relation = (
            "is the same as" if _comparable(left) == _comparable(right) else "is inside"
        )
        return (
            f"Moves overlap: {left_label} {left} {relation} "
            f"{right_label} {right}."
        )
    return None


def _merge_counts(into: dict[str, int], counts: dict[str, int]) -> None:
    """Add per-file counts into a running total.

    Summing rather than overwriting: two moves in one batch can each repair a
    reference in the same configuration file, and the manifest's top-level
    total has to say how many were repaired altogether rather than how many the
    last move happened to make.
    """
    for path, count in counts.items():
        into[path] = into.get(path, 0) + count


def _unique_directories(paths: Sequence[Path]) -> list[Path]:
    """De-duplicate directories case-insensitively, keeping the first spelling.

    Case-insensitively because two spellings of one directory name one
    directory on this filesystem, and creating it twice would fail on the
    second. First occurrence order is kept because the lists this is given are
    already ordered outermost first, and a directory shared by two moves is
    always reached through the same ancestors, so parents stay ahead of their
    children.
    """
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = _comparable(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def plan_moves(
    pairs: Sequence[tuple[str | Path, str | Path]],
    config: Config,
    home: Path,
    *,
    ps_output: str | None = None,
    create_parents: bool = False,
    operations: Operations | None = None,
) -> BatchPlan:
    """Decide what a batch of moves implies, touching nothing.

    Every guard the single-move flow applies is applied here in the same order,
    and every problem found is collected rather than raised on the spot, so a
    :class:`PlanError` carries one line per cause. The wording of each line is
    the single-move wording, which is what makes a batch of one print exactly
    what it prints today.

    The per-move previews are skipped when the running-IDE guard or the overlap
    guard has already found something. Both of those mean nothing in the batch
    can go ahead, and running the previews anyway would add a second cause per
    move to a message whose value is one cause a line.
    """
    if not pairs:
        raise PlanError(["No moves were given."])

    operations = operations or Operations()
    problems: list[str] = []

    checks = [
        check_move(source, dest, home, create_parents=create_parents)
        for source, dest in pairs
    ]
    usable: list[MoveCheck] = []
    for check in checks:
        if check.problem is not None:
            problems.append(check.problem)
        else:
            usable.append(check)

    # Once, not once per move: a running IDE is a fact about the machine, and
    # repeating it per move would bury the real per-move problems.
    blocked = False
    try:
        assert_no_ide_running(ps_output)
    except MigrateError as exc:
        problems.append(str(exc))
        blocked = True

    for index, first in enumerate(usable):
        for second in usable[index + 1 :]:
            overlap = _overlap_problem(first, second)
            if overlap is not None:
                problems.append(overlap)
                blocked = True

    products = find_product_dirs(config.jetbrains_root, config.exclude_products)
    moves: list[MovePlan] = []
    if not blocked:
        for check in usable:
            try:
                moves.append(_plan_one(check, config, products, operations))
            except MigrateError as exc:
                # Planning the rest is still worth doing: a batch usually fails
                # for one reason per move, and the user wants all of them at
                # once.
                problems.append(str(exc))

    if problems:
        raise PlanError(problems)

    return BatchPlan(
        # The home directory as the checks normalised it, rather than as the
        # caller spelled it, so the plan records the spelling every comparison
        # and every rewrite actually used.
        home=checks[0].home,
        config=config,
        products=products,
        moves=moves,
        missing_parents=_unique_directories(
            [parent for check in usable for parent in check.missing_parents]
        ),
        registry_path=config.claude_registry,
    )


def _plan_one(
    check: MoveCheck,
    config: Config,
    products: Sequence[Path],
    operations: Operations,
) -> MovePlan:
    """Run one move's previews, in the order the single-move flow runs them.

    Every one of these is a dry run, and each is here because it can fail: an
    entry name already taken at the destination, a transcript or a registry a
    rewrite would turn into invalid JSON, a destination whose name XML cannot
    hold raw. Discovering any of them now means the run stops while every
    directory is still in place and no backup exists yet.
    """
    variants = prefix_variants(check.source, check.dest, check.home)
    reference_count = count_references(products, variants)

    claude_variants = json_variants(check.source, check.dest)
    claude_plan = plan_entry_renames(config.claude_root, check.source, check.dest)
    claude_files = rewritable_files(config.claude_root, claude_plan.renames)
    claude_rewrites = operations.preview_rewrites(claude_files, claude_variants)
    registry_plan = plan_registry_renames(
        config.claude_registry, check.source, check.dest
    )
    operations.rewrite_registry(
        config.claude_registry, registry_plan.renames, dry_run=True
    )
    config_changes = operations.rewrite_products(products, variants, dry_run=True)
    cache_plan = plan_cache_renames(
        config.caches_root,
        [product.name for product in products],
        check.source,
        check.dest,
        variants,
    )

    return MovePlan(
        source=check.source,
        dest=check.dest,
        same_device=check.same_device,
        reference_count=reference_count,
        config_changes=config_changes,
        claude_renames=list(claude_plan.renames),
        claude_rewrites=claude_rewrites,
        claude_unmatched=list(claude_plan.unmatched),
        registry_renames=list(registry_plan.renames),
        cache_renames=list(cache_plan.renames),
        cache_unconfirmed=list(cache_plan.unconfirmed),
        cache_relink_required=cache_plan.relink_required,
    )


class BatchRun:
    """One execution of a :class:`BatchPlan`, from backup to final manifest.

    Constructing it touches nothing. :meth:`execute` does everything once; a
    second call is not supported, because the plan it holds describes the state
    of the machine before the first one.
    """

    def __init__(
        self,
        plan: BatchPlan,
        *,
        now: datetime,
        progress: Callable[[str], None] | None = None,
        operations: Operations | None = None,
    ) -> None:
        self.plan = plan
        self.now = now
        self.operations = operations or Operations()
        self._progress = progress
        self.state = RunState(pending=list(plan.moves))
        # The move being made right now, which is what a failure has to name,
        # and its position among the records.
        self._current: MovePlan | None = None
        self._current_index = 0
        # One record per move, in run order, kept in the shape the manifest
        # stores them so that writing it is a copy rather than a translation.
        self._records: list[dict] = []
        self._backed_up: list[str] = []

    def execute(self) -> RunResult:
        """Take the backup, make every move, and record what happened.

        A deliberate error or an operating system failure comes back as
        :class:`RunFailed` carrying the run's state. Anything else - a Ctrl-C,
        or a bug - propagates unchanged, because it is not this layer's to
        interpret; the state is filled in first either way, so the caller can
        say the same thing about recovery in both cases.
        """
        try:
            return self._run()
        except (MigrateError, OSError) as exc:
            self._record_failure()
            raise RunFailed(exc, self.state) from exc
        except BaseException:
            self._record_failure()
            raise

    def _run(self) -> RunResult:
        backup_dir = self._back_up()
        self._write_manifest()
        created = self._create_missing_parents()

        for index, move in enumerate(self.plan.moves):
            self._current = move
            self._current_index = index
            self.state.pending = list(self.plan.moves[index + 1 :])
            self.state.completed.append(self._make_move(move))
            self._current = None

        self.say("Scanning the moved projects for hardcoded paths...")
        self.state.completed = [
            replace(
                result,
                hardcoded_paths=find_hardcoded_paths(result.plan.dest, self.plan.home),
            )
            for result in self.state.completed
        ]
        self._write_manifest()

        return RunResult(
            backup_dir=backup_dir,
            moves=list(self.state.completed),
            created_dirs=created,
        )

    def say(self, message: str) -> None:
        """Report a phase that takes a while, if the caller asked to hear."""
        if self._progress is not None:
            self._progress(message)

    def _back_up(self) -> Path:
        """Save everything the run is about to change, before it changes.

        The Claude Code files are copied from their pre-rename locations, which
        is where they still are, and only the ones a preview says will actually
        change: an undo reverses an entry rename by renaming back, so copying a
        transcript tree that can run to hundreds of megabytes would buy
        nothing. The registry is the exception and is copied whole, because it
        is rewritten as a whole document rather than patched in place.
        """
        config = self.plan.config
        backup_dir = new_backup_dir(config.backup_root, self.now)
        self.state.backup_dir = backup_dir

        self.say(f"Backing up settings for {len(self.plan.products)} products...")
        self._backed_up = back_up_products(self.plan.products, backup_dir)
        write_undo_script(backup_dir)

        # dict.fromkeys de-duplicates while keeping the first occurrence's
        # position: the shared history file is in every move's list, and copying
        # it once per move would put the same bytes in the backup three times.
        back_up_claude_files(
            config.claude_root,
            list(
                dict.fromkeys(
                    Path(path)
                    for move in self.plan.moves
                    for path in move.claude_rewrites
                )
            ),
            backup_dir,
        )

        if any(move.registry_renames for move in self.plan.moves):
            back_up_registry(config.claude_registry, backup_dir)

        self._records = [
            {
                "source": str(move.source),
                "dest": str(move.dest),
                "status": "pending",
                "rewritten_files": {},
                # Recorded before the renames happen, so an interrupted run
                # still leaves undo a complete list of what it may have to
                # reverse.
                "claude_renames": [
                    [str(rename.old), str(rename.new)] for rename in move.claude_renames
                ],
                "claude_rewritten_files": {},
                "registry_renames": [
                    [rename.old, rename.new] for rename in move.registry_renames
                ],
                # Recorded before the renames happen, for the same reason the
                # Claude entries are: an interrupted run still leaves undo a
                # complete list of what it may have to reverse.
                "cache_renames": [
                    [str(rename.old), str(rename.new)]
                    for rename in move.cache_renames
                ],
                "cache_rewritten_files": {},
            }
            for move in self.plan.moves
        ]
        return backup_dir

    def _create_missing_parents(self) -> list[Path]:
        """Create the destination parents that do not exist, outermost first.

        Ordered rather than created with ``parents=True`` so that every
        directory this run brings into being is one the manifest already names:
        undo removes exactly those, and only while they are still empty.
        """
        created: list[Path] = []
        for directory in self.plan.missing_parents:
            directory.mkdir()
            created.append(directory)
        return created

    def _make_move(self, move: MovePlan) -> MoveResult:
        """Move one directory and repair every reference to its old location."""
        config = self.plan.config
        variants = prefix_variants(move.source, move.dest, self.plan.home)
        claude_variants = json_variants(move.source, move.dest)

        self.say(f"Moving {move.source} to {move.dest}...")
        self.operations.move_directory(
            MoveSpec(
                source=move.source,
                dest=move.dest,
                home=self.plan.home,
                same_device=move.same_device,
            )
        )

        self.say("Repairing path references in the IDE configuration...")
        rewritten = self.operations.rewrite_products(self.plan.products, variants)
        remaining = count_references(self.plan.products, variants)

        self.say("Relocating Claude Code project data...")
        claude_renamed = rename_entries(move.claude_renames)
        # The preview listed these files where they were before the rename, so
        # each path is mapped forward to where the rename has just put it.
        claude_rewritten = rewrite_files(
            [
                relocate(Path(path), move.claude_renames)
                for path in move.claude_rewrites
            ],
            claude_variants,
        )
        # The registry is re-planned against its current contents inside
        # rewrite_registry rather than trusted from the plan, because Claude
        # Code writes to it on every session and one may have finished since.
        # The count therefore comes back from the rewrite: it is what was
        # actually renamed, not what was intended.
        registry_renamed = self.operations.rewrite_registry(
            config.claude_registry, move.registry_renames
        )

        self.say("Relocating the IDE's stored module definitions...")
        cache_renamed = rename_caches(move.cache_renames)
        # Rewritten at the post-rename location, which is where the files are
        # by now; a rename that was skipped is not in the list, so nothing here
        # points at a directory that never moved.
        cache_rewritten = rewrite_caches(
            [Path(new) for _, new in cache_renamed], variants
        )

        record = self._records[self._current_index]
        record["status"] = "moved"
        record["rewritten_files"] = rewritten
        record["claude_rewritten_files"] = claude_rewritten
        record["cache_rewritten_files"] = cache_rewritten
        self._write_manifest()

        return MoveResult(
            plan=move,
            rewritten_files=rewritten,
            remaining=remaining,
            claude_renamed=claude_renamed,
            claude_rewritten=claude_rewritten,
            registry_renamed=registry_renamed,
            cache_renamed=cache_renamed,
            cache_rewritten=cache_rewritten,
            # Filled in once every move is done, so the scan runs in one pass.
            hardcoded_paths=[],
        )

    def _record_failure(self) -> None:
        """Write down how far the run got, for the caller to act on.

        Whether the failing move's directory had already left its source is
        read from the filesystem rather than inferred from where the error came
        from: an interrupted cross-device copy raises from inside the move with
        the source still intact, and a Ctrl-C a moment later leaves it gone.

        Marking the record in the manifest is attempted only when there is a
        manifest to mark, and a failure to write it is swallowed: the run is
        already failing, and replacing its error with a second one about the
        manifest would hide the thing the user has to fix.
        """
        move = self._current
        self.state.failed = move
        if move is not None:
            self.state.failed_moved = not move.source.exists()
            if self._records:
                self._records[self._current_index]["status"] = "failed"
        if self.state.backup_dir is not None and self._records:
            with contextlib.suppress(OSError):
                self._write_manifest()

    def _write_manifest(self) -> None:
        """Write the manifest as it stands, from the records kept so far.

        The top-level fields carry the first move's paths and the union of
        every move's counts and renames. That is what a reader written before
        batch runs existed sees, and it stays truthful for one: the same fields
        describe the same single move.
        """
        backup_dir = self.state.backup_dir
        if backup_dir is None or not self._records:
            return

        config = self.plan.config
        rewritten: dict[str, int] = {}
        claude_rewritten: dict[str, int] = {}
        cache_rewritten: dict[str, int] = {}
        for record in self._records:
            _merge_counts(rewritten, record["rewritten_files"])
            _merge_counts(claude_rewritten, record["claude_rewritten_files"])
            _merge_counts(cache_rewritten, record["cache_rewritten_files"])

        renames = [
            pair for record in self._records for pair in record["claude_renames"]
        ]
        registry_renames = [
            pair for record in self._records for pair in record["registry_renames"]
        ]
        cache_renames = [
            pair for record in self._records for pair in record["cache_renames"]
        ]

        write_manifest(
            backup_dir,
            Manifest(
                version=1,
                tool_version=__version__,
                created_at=self.now.isoformat(),
                home=str(self.plan.home),
                jetbrains_root=str(config.jetbrains_root),
                source=self._records[0]["source"],
                dest=self._records[0]["dest"],
                move_status=(
                    "moved"
                    if all(record["status"] == "moved" for record in self._records)
                    else "pending"
                ),
                backed_up_products=self._backed_up,
                rewritten_files=rewritten,
                undone_at=None,
                claude_root=str(config.claude_root),
                claude_renames=renames,
                claude_rewritten_files=claude_rewritten,
                # Named only when a key is going to move, because this field is
                # what tells every undo route that a saved registry exists to
                # put back. Naming a file the run never copied would have undo
                # look for a backup that is not there.
                claude_registry=(
                    str(config.claude_registry) if registry_renames else None
                ),
                claude_registry_renames=registry_renames,
                caches_root=str(config.caches_root),
                cache_renames=cache_renames,
                cache_rewritten_files=cache_rewritten,
                moves=[dict(record) for record in self._records],
                created_dirs=[str(path) for path in self.plan.missing_parents],
            ),
        )


@dataclass(frozen=True)
class UndoResult:
    """What an undo did, and the backup it did it from."""

    backup_dir: Path
    actions: list[str]


def undo_settings_root(backup_dir: Path, config: Config) -> Path:
    """Decide which settings directory an undo should restore into.

    The manifest records where the backup was actually taken from, and that
    wins. Someone who migrated with a --config pointing jetbrains_root at a
    non-standard location should not have to remember the same flag to undo,
    and restoring into the wrong directory would leave the settings they
    actually use still broken while reporting success. The standalone undo.sh
    reads the same recorded value, so both routes agree.

    A manifest written before that field existed does not say, and the
    configured value - the tool's own default unless --config says otherwise -
    is the best available guess.

    Reading the manifest here can fail: it may be missing or corrupt. That is
    not diagnosed here, because undo_backup checks it a moment later and
    reports it properly; the configured value simply stands in until then.
    """
    try:
        recorded = read_manifest(backup_dir).jetbrains_root
    except (OSError, ValueError, TypeError):
        return config.jetbrains_root
    return Path(recorded) if recorded else config.jetbrains_root


def undo_run(
    backup_dir: Path,
    config: Config,
    now: datetime,
    *,
    ps_output: str | None = None,
) -> UndoResult:
    """Roll back one run, whether it moved one directory or several.

    A batch needs no undo of its own: every run writes one manifest listing
    every move it made, and ``undo_backup`` reverses those in reverse order.
    """
    actions = undo_backup(
        backup_dir, undo_settings_root(backup_dir, config), now, ps_output
    )
    return UndoResult(backup_dir=backup_dir, actions=actions)
