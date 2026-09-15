"""Tests for the batch API: planning several moves, running them, undoing them.

Everything happens inside a temporary directory. The fake home holds three
projects to move, one JetBrains product, Claude Code's project data and its
per-project registry, so a single run exercises every side of a migration at
once. The real home directory is never read and never written.

Two properties get most of the attention here, because they are what a batch
adds over three separate runs. Planning reports every problem it found rather
than the first, and one run leaves one backup that reverses every move it made
- including a run that stopped half way through, and including through the
standalone undo.sh, which is the recovery route for when the tool itself is
what broke.
"""

import itertools
import json
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from idea_migrate.batch import (
    BatchRun,
    Operations,
    find_hardcoded_paths,
    plan_moves,
    undo_run,
)
from idea_migrate.claude_projects import encode_project_path
from idea_migrate.config import default_config
from idea_migrate.errors import MoveError, PathValidationError, PlanError, RunFailed
from idea_migrate.manifest import read_manifest
from idea_migrate.mover import move_directory
from idea_migrate.paths import check_move, validate_move
from idea_migrate.rewrite import rewrite_products

# Imported as modules rather than by name: pulling a TestCase class into this
# namespace would have the loader collect and run that whole class again here.
from tests import test_backup
from tests.test_roundtrip import snapshot

QUIET = "/usr/sbin/cfprefsd\n"
RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOW = datetime(2026, 9, 3, 11, 0, 0)
LATER = datetime(2026, 9, 4, 9, 0, 0)

PRODUCT = "IntelliJIdea2026.2"
NAMES = ("Alpha", "Beta", "Gamma")
# A sibling whose name starts with a project's but continues with a letter. It
# is in every part of the fixture - the settings, the Claude Code entries, the
# registry - because it is the case a plain prefix match would corrupt.
LOOKALIKE = "AlphaArchive"


def jsonl(*records: dict) -> str:
    """Render records the way Claude Code writes them: one JSON object a line."""
    return "".join(json.dumps(record) + "\n" for record in records)


class BatchTestCase(unittest.TestCase):
    """A fake home with three projects to move and everything that refers to them.

    The three destinations share two parent directories that do not exist yet,
    which is what the ``create_parents`` half of the API is for: the plan has to
    list each missing directory once, outermost first, and the run has to create
    them and record them so an undo can take them away again.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # The temporary root holds the fake home rather than being it, so the
        # process-table stubs the undo.sh tests put here are outside the tree
        # the before/after comparisons cover.
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._stub_counter = itertools.count()

        self.projects_root = self.home / "IdeaProjects"
        for name in (*NAMES, LOOKALIKE):
            (self.projects_root / name / ".idea").mkdir(parents=True)
            (self.projects_root / name / "main.py").write_text(
                f"# {name}\n", encoding="utf-8"
            )
        # One project keeps an absolute path to the home directory inside its
        # own .idea, which no rewrite touches. The scan at the end of a run is
        # what tells the user about it.
        (self.projects_root / "Alpha" / ".idea" / "misc.xml").write_text(
            f'<project><option value="{self.home}/sdk" /></project>\n',
            encoding="utf-8",
        )

        self.jetbrains = self.home / "Library" / "Application Support" / "JetBrains"
        product = self.jetbrains / PRODUCT
        (product / "options").mkdir(parents=True)
        (product / "workspace").mkdir(parents=True)
        self.recent_projects = product / "options" / "recentProjects.xml"
        self.recent_projects.write_text(
            "<application>\n"
            + "".join(
                f'  <entry key="$USER_HOME$/IdeaProjects/{name}" />\n'
                for name in (*NAMES, LOOKALIKE)
            )
            + '  <entry key="$USER_HOME$/Downloads/unrelated" />\n'
            "</application>\n",
            encoding="utf-8",
        )
        # Only one project is named here, so the two products' counts differ and
        # a per-move count that quietly totalled the batch would show up.
        self.workspace_file = product / "workspace" / "AAA.xml"
        self.workspace_file.write_text(
            '<project><component path="$USER_HOME$/IdeaProjects/Alpha" /></project>\n',
            encoding="utf-8",
        )

        self.claude_root = self.home / ".claude"
        self.claude_projects = self.claude_root / "projects"
        self.claude_projects.mkdir(parents=True)
        self.transcripts: dict[str, Path] = {}
        for name in (*NAMES, LOOKALIKE):
            entry = self.claude_projects / encode_project_path(
                self.projects_root / name
            )
            entry.mkdir()
            transcript = entry / f"session-{name.lower()}.jsonl"
            transcript.write_text(
                jsonl(
                    {
                        "type": "user",
                        "cwd": str(self.projects_root / name),
                        "sessionId": name.lower(),
                    }
                ),
                encoding="utf-8",
            )
            self.transcripts[name] = transcript

        self.unrelated = self.home / "Downloads" / "unrelated"
        self.history = self.claude_root / "history.jsonl"
        self.history.write_text(
            jsonl(
                *[
                    {
                        "display": f"work on {name}",
                        "project": str(self.projects_root / name),
                        "sessionId": name.lower(),
                        "timestamp": index,
                    }
                    for index, name in enumerate((*NAMES, LOOKALIKE))
                ],
                {
                    "display": "somewhere else entirely",
                    "project": str(self.unrelated),
                    "sessionId": "s9",
                    "timestamp": 9,
                },
            ),
            encoding="utf-8",
        )

        # Claude Code's per-project registry, owner-only because it holds every
        # project's tool permissions.
        self.registry = self.home / ".claude.json"
        self.registry_projects = {
            **{
                str(self.projects_root / name): {"allowedTools": [f"Read({name})"]}
                for name in (*NAMES, LOOKALIKE)
            },
            str(self.unrelated): {"allowedTools": ["Grep"]},
        }
        self.registry.write_text(
            json.dumps({"projects": self.registry_projects}, indent=2),
            encoding="utf-8",
        )
        self.registry.chmod(0o600)

        self.config = default_config(self.home)
        self.backup_root = self.config.backup_root
        self.destination_root = self.home / "Projects" / "cell" / "components"
        self.pairs = [
            (self.source_of(name), self.dest_of(name)) for name in NAMES
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def source_of(self, name: str) -> Path:
        return self.projects_root / name

    def dest_of(self, name: str) -> Path:
        return self.destination_root / name

    def entry_of(self, project: Path) -> Path:
        """The ``~/.claude/projects`` directory standing for a project."""
        return self.claude_projects / encode_project_path(project)

    def make_plan(self, pairs=None, **kwargs):
        return plan_moves(
            pairs if pairs is not None else self.pairs,
            self.config,
            self.home,
            ps_output=kwargs.pop("ps_output", QUIET),
            create_parents=kwargs.pop("create_parents", True),
            **kwargs,
        )

    def execute(self, plan=None, **kwargs):
        """Run a plan, keeping the progress messages it emitted."""
        self.messages: list[str] = []
        run = BatchRun(
            plan if plan is not None else self.make_plan(),
            now=NOW,
            progress=self.messages.append,
            **kwargs,
        )
        self.run_object = run
        return run.execute()

    def only_backup(self) -> Path:
        backups = sorted(self.backup_root.iterdir())
        self.assertEqual(len(backups), 1, backups)
        return backups[0]

    def registry_keys(self) -> list[str]:
        return list(json.loads(self.registry.read_text(encoding="utf-8"))["projects"])

    def take_snapshot(self) -> dict[str, str]:
        return snapshot(self.home, self.backup_root)


class TestPlanMoves(BatchTestCase):
    def test_one_plan_per_move_in_the_order_given(self):
        plan = self.make_plan()

        self.assertEqual(
            [(move.source, move.dest) for move in plan.moves],
            [(self.source_of(name), self.dest_of(name)) for name in NAMES],
        )
        self.assertEqual(plan.home, self.home)
        self.assertEqual(plan.products, [self.jetbrains / PRODUCT])
        self.assertEqual(plan.registry_path, self.registry)

    def test_the_counts_are_per_move_and_not_the_batch_total(self):
        """Each move is measured on its own references, not on the batch's.

        Alpha is named in both the recent-projects list and the workspace file
        while the other two are named only in the first, so a count that had
        quietly totalled the batch - or copied one move's answer to the rest -
        would not survive this.
        """
        alpha, beta, gamma = self.make_plan().moves

        self.assertEqual(alpha.reference_count, 2)
        self.assertEqual(beta.reference_count, 1)
        self.assertEqual(gamma.reference_count, 1)
        self.assertEqual(self.make_plan().total_references, 4)
        self.assertEqual(
            alpha.config_changes,
            {str(self.recent_projects): 1, str(self.workspace_file): 1},
        )
        self.assertEqual(beta.config_changes, {str(self.recent_projects): 1})

    def test_the_claude_side_of_each_move_is_planned_too(self):
        for move, name in zip(self.make_plan().moves, NAMES):
            with self.subTest(name=name):
                self.assertEqual(
                    [(rename.old, rename.new) for rename in move.claude_renames],
                    [(self.entry_of(move.source), self.entry_of(move.dest))],
                )
                # The project's own transcript and the shared history file. The
                # history is in every move's list, because every move has a
                # line in it.
                self.assertEqual(
                    move.claude_rewrites,
                    {str(self.history): 1, str(self.transcripts[name]): 1},
                )
                self.assertEqual(
                    [(rename.old, rename.new) for rename in move.registry_renames],
                    [(str(move.source), str(move.dest))],
                )
                self.assertEqual(move.claude_unmatched, [])

    def test_missing_parents_are_listed_once_outermost_first(self):
        """The three destinations share their missing parents.

        They have to be created in this order - a directory cannot be made
        before its parent - and named once each, because the manifest records
        them for undo to remove and a repeated directory would be a repeated
        removal.
        """
        plan = self.make_plan()

        self.assertEqual(
            plan.missing_parents,
            [
                self.home / "Projects",
                self.home / "Projects" / "cell",
                self.home / "Projects" / "cell" / "components",
            ],
        )

    def test_planning_writes_nothing(self):
        before = self.take_snapshot()
        self.make_plan()
        self.assertEqual(self.take_snapshot(), before)
        self.assertFalse(self.backup_root.exists())


class TestPlanWithParentsAlreadyThere(BatchTestCase):
    """The same batch when the destinations' parents already exist."""

    def setUp(self):
        super().setUp()
        self.destination_root.mkdir(parents=True)

    def test_nothing_has_to_be_created(self):
        plan = self.make_plan(create_parents=False)
        self.assertEqual(plan.missing_parents, [])
        self.assertEqual(len(plan.moves), 3)

    def test_the_run_creates_nothing_and_records_nothing(self):
        result = self.execute(self.make_plan(create_parents=False))
        self.assertEqual(result.created_dirs, [])
        self.assertEqual(read_manifest(result.backup_dir).created_dirs, [])


class TestPlanGuards(BatchTestCase):
    def assert_problems(self, pairs, expected, **kwargs):
        """Plan a batch that must be refused, and return the reasons given."""
        with self.assertRaises(PlanError) as caught:
            self.make_plan(pairs, **kwargs)
        problems = caught.exception.problems
        self.assertEqual(len(problems), expected, problems)
        # The message is the reasons joined by newlines, which is what makes a
        # batch of one print exactly the line a single move prints.
        self.assertEqual(str(caught.exception), "\n".join(problems))
        return problems

    def test_a_running_ide_stops_the_whole_batch(self):
        problems = self.assert_problems(self.pairs, 1, ps_output=RUNNING)
        self.assertIn("are running", problems[0])

    def test_a_destination_that_already_exists(self):
        self.dest_of("Beta").mkdir(parents=True)
        problems = self.assert_problems(self.pairs, 1)
        self.assertEqual(
            problems[0], f"Destination already exists: {self.dest_of('Beta')}"
        )

    def test_a_source_that_is_not_there(self):
        missing = self.projects_root / "Delta"
        problems = self.assert_problems(
            [*self.pairs, (missing, self.dest_of("Delta"))], 1
        )
        self.assertEqual(problems[0], f"Source does not exist: {missing}")

    def test_one_source_inside_another(self):
        problems = self.assert_problems(
            [
                (self.projects_root, self.home / "Elsewhere"),
                (self.source_of("Alpha"), self.dest_of("Alpha")),
            ],
            1,
        )
        self.assertEqual(
            problems[0],
            f"Moves overlap: source {self.source_of('Alpha')} is inside "
            f"source {self.projects_root}.",
        )

    def test_two_moves_to_the_same_destination(self):
        problems = self.assert_problems(
            [
                (self.source_of("Alpha"), self.dest_of("Shared")),
                (self.source_of("Beta"), self.dest_of("Shared")),
            ],
            1,
        )
        self.assertEqual(
            problems[0],
            f"Moves overlap: destination {self.dest_of('Shared')} is the same as "
            f"destination {self.dest_of('Shared')}.",
        )

    def test_a_destination_inside_another_move_s_source(self):
        """One move would land inside a directory another move is taking away.

        Whichever ran first would break the other, so the pair is refused
        rather than ordered around.
        """
        inside = self.source_of("Beta") / "nested"
        problems = self.assert_problems(
            [
                (self.source_of("Alpha"), inside),
                (self.source_of("Beta"), self.dest_of("Beta")),
            ],
            1,
        )
        self.assertEqual(
            problems[0],
            f"Moves overlap: destination {inside} is inside "
            f"source {self.source_of('Beta')}.",
        )

    def test_every_problem_is_reported_at_once(self):
        """Three faults in one batch come back as three lines, not as the first.

        A user fixing a batch wants the whole list now; discovering the next
        problem on each attempt is what this API exists to avoid.
        """
        missing = self.projects_root / "Delta"
        self.dest_of("Beta").mkdir(parents=True)
        problems = self.assert_problems(
            [
                (missing, self.dest_of("Delta")),
                (self.source_of("Beta"), self.dest_of("Beta")),
                (self.source_of("Alpha"), self.dest_of("Shared")),
                (self.source_of("Gamma"), self.dest_of("Shared")),
            ],
            3,
        )
        self.assertEqual(problems[0], f"Source does not exist: {missing}")
        self.assertEqual(
            problems[1], f"Destination already exists: {self.dest_of('Beta')}"
        )
        self.assertIn("Moves overlap", problems[2])

    def test_a_batch_of_one_says_exactly_what_a_single_move_says(self):
        missing = self.projects_root / "Delta"
        with self.assertRaises(PathValidationError) as single:
            validate_move(missing, self.dest_of("Delta"), self.home)
        with self.assertRaises(PlanError) as batch:
            self.make_plan([(missing, self.dest_of("Delta"))])

        self.assertEqual(batch.exception.problems, [str(single.exception)])
        self.assertEqual(str(batch.exception), str(single.exception))

    def test_nothing_is_touched_by_a_refused_plan(self):
        before = self.take_snapshot()
        with self.assertRaises(PlanError):
            self.make_plan(ps_output=RUNNING)
        self.assertEqual(self.take_snapshot(), before)


class TestExecute(BatchTestCase):
    def setUp(self):
        super().setUp()
        self.before = self.take_snapshot()
        self.result = self.execute()
        self.backup_dir = self.only_backup()
        self.manifest = read_manifest(self.backup_dir)

    def test_every_directory_moved(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertFalse(self.source_of(name).exists())
                self.assertTrue(self.dest_of(name).is_dir())
                self.assertEqual(
                    (self.dest_of(name) / "main.py").read_text(encoding="utf-8"),
                    f"# {name}\n",
                )
        # The sibling whose name merely starts the same way stayed put.
        self.assertTrue((self.projects_root / LOOKALIKE).is_dir())

    def test_one_backup_holds_the_whole_batch(self):
        self.assertTrue((self.backup_dir / "undo.sh").is_file())
        self.assertTrue((self.backup_dir / "manifest.json").is_file())
        # Copied once, not once per move, even though all three renamed keys.
        self.assertTrue((self.backup_dir / "claude-registry.json").is_file())
        saved = self.backup_dir / "claude"
        self.assertTrue((saved / "history.jsonl").is_file())
        self.assertEqual(len(sorted(saved.rglob("*.jsonl"))), 4)

    def test_the_manifest_records_one_completed_move_each(self):
        records = self.manifest.move_records()
        self.assertEqual([record.status for record in records], ["moved"] * 3)
        self.assertEqual(
            [(record.source, record.dest) for record in records],
            [(str(self.source_of(name)), str(self.dest_of(name))) for name in NAMES],
        )
        self.assertEqual(self.manifest.move_status, "moved")
        self.assertEqual(self.manifest.source, str(self.source_of("Alpha")))
        self.assertEqual(self.manifest.dest, str(self.dest_of("Alpha")))

    def test_the_top_level_fields_hold_the_union_of_every_move(self):
        """What an older reader sees has to be the whole batch, not one move.

        The recent-projects file was repaired by all three moves and the shared
        history file by all three too, so their counts are sums rather than the
        last move's answer.
        """
        self.assertEqual(
            self.manifest.rewritten_files,
            {str(self.recent_projects): 3, str(self.workspace_file): 1},
        )
        self.assertEqual(self.manifest.claude_rewritten_files[str(self.history)], 3)
        self.assertEqual(len(self.manifest.claude_renames), 3)
        self.assertEqual(len(self.manifest.claude_registry_renames), 3)
        self.assertEqual(self.manifest.claude_registry, str(self.registry))
        self.assertEqual(self.manifest.claude_root, str(self.claude_root))
        self.assertEqual(self.manifest.jetbrains_root, str(self.jetbrains))

    def test_the_created_directories_are_recorded(self):
        self.assertEqual(
            self.manifest.created_dirs,
            [
                str(self.home / "Projects"),
                str(self.home / "Projects" / "cell"),
                str(self.destination_root),
            ],
        )
        self.assertEqual(
            self.result.created_dirs,
            [
                self.home / "Projects",
                self.home / "Projects" / "cell",
                self.destination_root,
            ],
        )
        self.assertTrue(self.destination_root.is_dir())

    def test_the_settings_point_at_the_new_locations(self):
        text = self.recent_projects.read_text(encoding="utf-8")
        for name in NAMES:
            with self.subTest(name=name):
                self.assertIn(f"$USER_HOME$/Projects/cell/components/{name}", text)
                self.assertNotIn(f'"$USER_HOME$/IdeaProjects/{name}"', text)
        self.assertIn(f"$USER_HOME$/IdeaProjects/{LOOKALIKE}", text)
        self.assertIn("$USER_HOME$/Downloads/unrelated", text)

    def test_the_claude_entries_and_transcripts_followed(self):
        for name in NAMES:
            with self.subTest(name=name):
                moved = self.entry_of(self.dest_of(name))
                self.assertTrue(moved.is_dir())
                self.assertFalse(self.entry_of(self.source_of(name)).exists())
                record = json.loads(
                    (moved / f"session-{name.lower()}.jsonl").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(record["cwd"], str(self.dest_of(name)))
        self.assertTrue(self.entry_of(self.projects_root / LOOKALIKE).is_dir())

        history = [
            json.loads(line)
            for line in self.history.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["project"] for record in history],
            [str(self.dest_of(name)) for name in NAMES]
            + [str(self.projects_root / LOOKALIKE), str(self.unrelated)],
        )

    def test_the_registry_keys_followed_and_the_file_stayed_private(self):
        self.assertEqual(
            self.registry_keys(),
            [str(self.dest_of(name)) for name in NAMES]
            + [str(self.projects_root / LOOKALIKE), str(self.unrelated)],
        )
        # The settings objects came across untouched.
        moved = json.loads(self.registry.read_text(encoding="utf-8"))["projects"]
        self.assertEqual(
            moved[str(self.dest_of("Alpha"))], {"allowedTools": ["Read(Alpha)"]}
        )
        self.assertEqual(stat.S_IMODE(self.registry.lstat().st_mode), 0o600)

    def test_the_result_describes_each_move(self):
        self.assertEqual(self.result.backup_dir, self.backup_dir)
        self.assertEqual(len(self.result.moves), 3)
        alpha, beta, _gamma = self.result.moves
        self.assertEqual(
            alpha.rewritten_files,
            {str(self.recent_projects): 1, str(self.workspace_file): 1},
        )
        self.assertEqual(beta.rewritten_files, {str(self.recent_projects): 1})
        self.assertEqual([move.remaining for move in self.result.moves], [0, 0, 0])
        self.assertEqual(
            [move.registry_renamed for move in self.result.moves], [1, 1, 1]
        )
        self.assertEqual(
            alpha.claude_renamed,
            [
                (
                    str(self.entry_of(self.source_of("Alpha"))),
                    str(self.entry_of(alpha.plan.dest)),
                )
            ],
        )
        # The rewritten Claude Code files are named where the rename has just
        # put them, not where the preview found them.
        self.assertEqual(
            set(alpha.claude_rewritten),
            {
                str(self.history),
                str(self.entry_of(alpha.plan.dest) / "session-alpha.jsonl"),
            },
        )
        # Only Alpha's own .idea keeps an absolute path to the home directory.
        self.assertEqual(
            alpha.hardcoded_paths,
            [str(self.dest_of("Alpha") / ".idea" / "misc.xml")],
        )
        self.assertEqual(beta.hardcoded_paths, [])

    def test_the_run_state_ends_with_everything_completed(self):
        state = self.run_object.state
        self.assertEqual(len(state.completed), 3)
        self.assertIsNone(state.failed)
        self.assertFalse(state.failed_moved)
        self.assertEqual(state.pending, [])
        self.assertTrue(state.anything_moved)

    def test_the_progress_messages_are_the_ones_the_cli_prints(self):
        self.assertEqual(
            self.messages,
            ["Backing up settings for 1 products..."]
            + [
                message
                for name in NAMES
                for message in (
                    f"Moving {self.source_of(name)} to {self.dest_of(name)}...",
                    "Repairing path references in the IDE configuration...",
                    "Relocating Claude Code project data...",
                    "Relocating the IDE's stored module definitions...",
                )
            ]
            + ["Scanning the moved projects for hardcoded paths..."],
        )


class TestRoundTrip(BatchTestCase):
    """A run and its undo, both ways round, must leave the home exactly as found."""

    def test_undo_run_restores_everything(self):
        before = self.take_snapshot()
        registry_before = self.registry.read_bytes()

        result = self.execute()
        self.assertNotEqual(self.take_snapshot(), before)

        undone = undo_run(result.backup_dir, self.config, LATER, ps_output=QUIET)

        self.assertEqual(undone.backup_dir, result.backup_dir)
        self.assertTrue(any("Moved" in line for line in undone.actions))
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual(self.take_snapshot(), before)
        # The created parents went with it, so the home has no new directories.
        self.assertFalse((self.home / "Projects").exists())

    def test_the_standalone_script_restores_everything_too(self):
        before = self.take_snapshot()
        result = self.execute()

        stub = self._stub_process_tools(
            test_backup.TestUndoScriptBehavior.NOTHING_RUNNING
        )
        script = self._run_script(result.backup_dir / "undo.sh", stub)

        self.assertEqual(script.returncode, 0, script.stderr)
        self.assertEqual(self.take_snapshot(), before)

    # The fake ``ps`` and ``pgrep`` are borrowed rather than copied: they are
    # what keep this test off the real process table, and there should be
    # exactly one of them in the suite.
    _stub_process_tools = test_backup.TestUndoScriptBehavior._stub_process_tools
    _run_script = test_backup.TestUndoScriptBehavior._run_script


class TestPartialFailure(BatchTestCase):
    """A batch that stops half way through, and what is left to work with."""

    def failing_on_beta(self) -> Operations:
        """Operations whose move of the second project fails outright.

        The failure lands before the directory has gone anywhere, which is the
        cheaper half of a partial failure: one move is done, one never started,
        and the one in between changed nothing.
        """

        def move(spec):
            if spec.source == self.source_of("Beta"):
                raise MoveError("simulated failure moving Beta")
            move_directory(spec)

        return Operations(move_directory=move)

    def failing_after_beta_moved(self) -> Operations:
        """Operations that fail once Beta's directory has already left.

        The second real rewrite is Beta's, and by then the directory is at its
        destination - the case where the state has to say so, because the
        recovery advice depends on it.
        """
        calls = []

        def rewrite(products, variants, dry_run=False):
            if dry_run:
                return rewrite_products(products, variants, dry_run=True)
            calls.append(variants)
            if len(calls) == 2:
                raise MoveError("simulated failure repairing the settings")
            return rewrite_products(products, variants)

        return Operations(rewrite_products=rewrite)

    def test_the_state_says_how_far_the_run_got(self):
        with self.assertRaises(RunFailed) as caught:
            self.execute(operations=self.failing_on_beta())

        state = caught.exception.state
        self.assertEqual(len(state.completed), 1)
        self.assertEqual(state.completed[0].plan.source, self.source_of("Alpha"))
        self.assertEqual(state.failed.source, self.source_of("Beta"))
        self.assertFalse(state.failed_moved)
        self.assertEqual(
            [move.source for move in state.pending], [self.source_of("Gamma")]
        )
        self.assertTrue(state.anything_moved)
        self.assertEqual(state.backup_dir, self.only_backup())
        # The message is the underlying failure's own, not a wrapper's.
        self.assertEqual(str(caught.exception), "simulated failure moving Beta")

    def test_the_manifest_records_what_each_move_did(self):
        with self.assertRaises(RunFailed):
            self.execute(operations=self.failing_on_beta())

        manifest = read_manifest(self.only_backup())
        self.assertEqual(
            [record.status for record in manifest.move_records()],
            ["moved", "failed", "pending"],
        )
        # Not "moved" at the top level: the batch as a whole did not finish.
        self.assertEqual(manifest.move_status, "pending")

    def test_undo_restores_the_snapshot_after_a_failure_before_the_move(self):
        before = self.take_snapshot()
        with self.assertRaises(RunFailed):
            self.execute(operations=self.failing_on_beta())

        undo_run(self.only_backup(), self.config, LATER, ps_output=QUIET)
        self.assertEqual(self.take_snapshot(), before)

    def test_a_failure_after_the_directory_moved_says_so(self):
        before = self.take_snapshot()
        with self.assertRaises(RunFailed) as caught:
            self.execute(operations=self.failing_after_beta_moved())

        state = caught.exception.state
        self.assertEqual(state.failed.source, self.source_of("Beta"))
        self.assertTrue(state.failed_moved)
        self.assertTrue(self.dest_of("Beta").is_dir())

        undo_run(self.only_backup(), self.config, LATER, ps_output=QUIET)
        self.assertEqual(self.take_snapshot(), before)

    def test_a_keyboard_interrupt_is_not_turned_into_something_else(self):
        """Ctrl-C propagates unchanged, with the state filled in on the way out.

        A caller has to be able to print the same recovery advice for an
        interrupted run as for a failed one, and a bug has to keep its own
        traceback rather than being reported as a migration error.
        """

        def move(spec):
            if spec.source == self.source_of("Beta"):
                raise KeyboardInterrupt
            move_directory(spec)

        with self.assertRaises(KeyboardInterrupt):
            self.execute(operations=Operations(move_directory=move))

        state = self.run_object.state
        self.assertEqual(len(state.completed), 1)
        self.assertEqual(state.failed.source, self.source_of("Beta"))
        self.assertFalse(state.failed_moved)
        self.assertEqual(len(state.pending), 1)


class TestCheckMove(BatchTestCase):
    def test_a_usable_move_comes_back_without_a_problem(self):
        check = check_move(
            self.source_of("Alpha"), self.home / "Moved-Alpha", self.home
        )
        self.assertIsNone(check.problem)
        self.assertEqual(check.missing_parents, ())
        self.assertTrue(check.same_device)

    def test_missing_parents_are_listed_outermost_first(self):
        check = check_move(
            self.source_of("Alpha"),
            self.dest_of("Alpha"),
            self.home,
            create_parents=True,
        )
        self.assertIsNone(check.problem)
        self.assertEqual(
            check.missing_parents,
            (
                self.home / "Projects",
                self.home / "Projects" / "cell",
                self.destination_root,
            ),
        )
        self.assertTrue(check.same_device)
        # Asking about them did not create them.
        self.assertFalse((self.home / "Projects").exists())

    def test_without_create_parents_the_message_is_the_one_it_always_was(self):
        dest = self.dest_of("Alpha")
        check = check_move(self.source_of("Alpha"), dest, self.home)
        with self.assertRaises(PathValidationError) as raised:
            validate_move(self.source_of("Alpha"), dest, self.home)
        self.assertEqual(check.problem, str(raised.exception))
        self.assertEqual(
            check.problem,
            f"Destination's parent directory does not exist: {dest.parent}",
        )

    def test_a_file_in_the_way_is_still_refused(self):
        """Parents can only be created under a directory, not under a file."""
        blocker = self.home / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        dest = blocker / "cell" / "Alpha"

        check = check_move(
            self.source_of("Alpha"), dest, self.home, create_parents=True
        )
        self.assertEqual(
            check.problem,
            f"Destination's parent directory does not exist: {dest.parent}",
        )

    def test_a_refused_move_still_names_its_paths(self):
        missing = self.projects_root / "Delta"
        check = check_move(missing, self.dest_of("Delta"), self.home)
        self.assertEqual(check.source, missing)
        self.assertEqual(check.dest, self.dest_of("Delta"))
        self.assertEqual(check.problem, f"Source does not exist: {missing}")


class TestFindHardcodedPaths(BatchTestCase):
    def test_only_xml_under_a_dot_idea_directory_counts(self):
        project = self.source_of("Alpha")
        (project / "notes.xml").write_text(f"{self.home}\n", encoding="utf-8")
        (project / ".idea" / "other.txt").write_text(f"{self.home}\n", encoding="utf-8")

        self.assertEqual(
            find_hardcoded_paths(project, self.home),
            [str(project / ".idea" / "misc.xml")],
        )

    def test_a_project_with_nothing_to_report(self):
        self.assertEqual(find_hardcoded_paths(self.source_of("Beta"), self.home), [])


if __name__ == "__main__":
    unittest.main()
