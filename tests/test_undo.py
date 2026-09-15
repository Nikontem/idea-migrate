import json
import shutil
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from idea_migrate.claude_projects import encode_project_path
from idea_migrate.errors import IdeRunningError, UndoError
from idea_migrate.manifest import Manifest, read_manifest, write_manifest
from idea_migrate.undo import undo_backup

QUIET = "/usr/sbin/cfprefsd\n"
RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOW = datetime(2026, 8, 30, 9, 0, 0)


class UndoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.jetbrains = self.home / "Library" / "Application Support" / "JetBrains"
        self.product = self.jetbrains / "IntelliJIdea2026.2"
        (self.product / "options").mkdir(parents=True)
        (self.product / "options" / "recentProjects.xml").write_text(
            "AFTER", encoding="utf-8"
        )

        self.source = self.home / "WebstormProjects"
        self.dest = self.home / "Projects" / "WebstormProjects"
        self.dest.mkdir(parents=True)
        (self.dest / "alpha.txt").write_text("hello", encoding="utf-8")

        self.backup_dir = self.tmp / "backups" / "2026-08-29_143005"
        saved = self.backup_dir / "config" / "IntelliJIdea2026.2" / "options"
        saved.mkdir(parents=True)
        (saved / "recentProjects.xml").write_text("BEFORE", encoding="utf-8")
        write_manifest(
            self.backup_dir,
            Manifest(
                version=1,
                tool_version="0.1.0",
                created_at="2026-08-29T14:30:05",
                home=str(self.home),
                jetbrains_root=str(self.jetbrains),
                source=str(self.source),
                dest=str(self.dest),
                move_status="moved",
                backed_up_products=["IntelliJIdea2026.2"],
                rewritten_files={"x.xml": 2},
                undone_at=None,
            ),
        )

    def tearDown(self):
        self._tmp.cleanup()


class TestUndoBackup(UndoTestCase):
    def test_moves_the_directory_back(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertEqual(
            (self.source / "alpha.txt").read_text(encoding="utf-8"), "hello"
        )

    def test_restores_the_configuration(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        restored = self.product / "options" / "recentProjects.xml"
        self.assertEqual(restored.read_text(encoding="utf-8"), "BEFORE")

    def test_marks_the_manifest_as_undone(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertEqual(read_manifest(self.backup_dir).undone_at, NOW.isoformat())

    def test_refuses_to_undo_twice(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        with self.assertRaises(UndoError) as ctx:
            undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertIn("already", str(ctx.exception).lower())

    def test_refuses_while_an_ide_is_running(self):
        with self.assertRaises(IdeRunningError):
            undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=RUNNING)

    def test_missing_backup_dir_raises(self):
        with self.assertRaises(UndoError):
            undo_backup(self.tmp / "absent", self.jetbrains, NOW, ps_output=QUIET)

    def test_backup_is_not_deleted(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(self.backup_dir.is_dir())
        self.assertTrue((self.backup_dir / "manifest.json").is_file())

    def test_a_file_created_after_the_migration_survives_the_undo(self):
        """Restoring settings is a merge, not a wholesale replacement.

        This is a deliberate design decision, so it is pinned here. Between
        the migration and the undo an IDE may write settings files that did
        not exist when the backup was taken - a new plugin's options, say.
        Undo overwrites the files it captured and leaves everything else
        alone. Replacing the settings directory outright instead would make
        an undo silently destroy unrelated work the user never asked to roll
        back.
        """
        created_later = self.product / "options" / "brand-new-plugin.xml"
        created_later.write_text("CREATED AFTER THE MIGRATION", encoding="utf-8")

        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(created_later.is_file())
        self.assertEqual(
            created_later.read_text(encoding="utf-8"),
            "CREATED AFTER THE MIGRATION",
        )
        # And the file that was backed up is still rolled back.
        self.assertEqual(
            (self.product / "options" / "recentProjects.xml").read_text(
                encoding="utf-8"
            ),
            "BEFORE",
        )

    def test_declines_the_move_but_still_restores_settings_when_source_exists(self):
        self.source.mkdir(parents=True)
        (self.source / "already-here.txt").write_text(
            "pre-existing", encoding="utf-8"
        )

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(self.source.is_dir())
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(
            (self.source / "already-here.txt").read_text(encoding="utf-8"),
            "pre-existing",
        )
        self.assertEqual(
            (self.dest / "alpha.txt").read_text(encoding="utf-8"), "hello"
        )
        self.assertTrue(any("already exists" in line for line in lines))
        restored = self.product / "options" / "recentProjects.xml"
        self.assertEqual(restored.read_text(encoding="utf-8"), "BEFORE")

    def test_declines_the_move_but_still_restores_settings_when_dest_missing(self):
        shutil.rmtree(self.dest)

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertFalse(self.dest.exists())
        self.assertFalse(self.source.exists())
        self.assertTrue(self.backup_dir.is_dir())
        self.assertTrue((self.backup_dir / "manifest.json").is_file())
        self.assertTrue(
            (self.backup_dir / "config" / "IntelliJIdea2026.2" / "options").is_dir()
        )
        self.assertTrue(any("not found" in line for line in lines))
        restored = self.product / "options" / "recentProjects.xml"
        self.assertEqual(restored.read_text(encoding="utf-8"), "BEFORE")


class ClaudeUndoTestCase(UndoTestCase):
    """The state a migration leaves behind for Claude Code's project data.

    On disk: the entry directory already carries its post-migration name and
    the transcript inside it already mentions the new location, as does the
    shared history file. In the backup: copies of exactly those two files, at
    the paths they had *before* the entry was renamed.

    Everything is inside a temporary directory. The real ``~/.claude`` is never
    read or written.
    """

    BEFORE = '{"cwd": "/before"}\n'
    AFTER = '{"cwd": "/after"}\n'
    HISTORY_BEFORE = '{"project": "/before"}\n'
    HISTORY_AFTER = '{"project": "/after"}\n'
    TRANSCRIPT = "session.jsonl"

    def _registry_text(self, project: Path) -> str:
        """One registry document, keyed by a project's absolute path."""
        return json.dumps(
            {"projects": {str(project): {"allowedTools": ["Bash"]}}},
            indent=2,
            ensure_ascii=False,
        )

    def setUp(self):
        super().setUp()
        self.claude_root = self.home / ".claude"
        self.projects = self.claude_root / "projects"
        self.projects.mkdir(parents=True)

        self.old_entry = self.projects / encode_project_path(self.source)
        self.new_entry = self.projects / encode_project_path(self.dest)
        self.new_entry.mkdir()
        (self.new_entry / self.TRANSCRIPT).write_text(self.AFTER, encoding="utf-8")

        self.history = self.claude_root / "history.jsonl"
        self.history.write_text(self.HISTORY_AFTER, encoding="utf-8")

        saved = self.backup_dir / "claude"
        saved_entry = saved / "projects" / self.old_entry.name
        saved_entry.mkdir(parents=True)
        (saved_entry / self.TRANSCRIPT).write_text(self.BEFORE, encoding="utf-8")
        (saved / "history.jsonl").write_text(self.HISTORY_BEFORE, encoding="utf-8")

        # Claude Code's per-project registry, in the state the migration left
        # it: the key names the new location. The backup holds the file as it
        # was, keyed by the old one, because the registry is restored whole
        # rather than merged. It is owner-only, as the real one is, so a
        # restore that widened it would show up here.
        self.registry = self.home / ".claude.json"
        self.registry.write_text(self._registry_text(self.dest), encoding="utf-8")
        self.registry.chmod(0o600)
        self.saved_registry = self.backup_dir / "claude-registry.json"
        self.saved_registry.write_text(
            self._registry_text(self.source), encoding="utf-8"
        )
        self.saved_registry.chmod(0o600)

        self.plain_manifest = read_manifest(self.backup_dir)
        write_manifest(
            self.backup_dir,
            replace(
                self.plain_manifest,
                claude_root=str(self.claude_root),
                claude_renames=[[str(self.old_entry), str(self.new_entry)]],
                claude_rewritten_files={
                    str(self.new_entry / self.TRANSCRIPT): 1,
                    str(self.history): 1,
                },
                claude_registry=str(self.registry),
                claude_registry_renames=[[str(self.source), str(self.dest)]],
            ),
        )


class TestUndoRestoresClaudeData(ClaudeUndoTestCase):
    def test_renames_the_entry_back(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(self.old_entry.is_dir())
        self.assertFalse(self.new_entry.exists())

    def test_restores_the_rewritten_files(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertEqual(
            (self.old_entry / self.TRANSCRIPT).read_text(encoding="utf-8"),
            self.BEFORE,
        )
        self.assertEqual(
            self.history.read_text(encoding="utf-8"), self.HISTORY_BEFORE
        )

    def test_says_what_it_did(self):
        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(
            any(
                f"Renamed Claude Code entry {self.new_entry} back to "
                f"{self.old_entry}" == line
                for line in lines
            ),
            lines,
        )
        self.assertTrue(any("Restored Claude Code files" in line for line in lines))

    def test_a_file_written_after_the_migration_survives(self):
        """Restoring Claude data is a merge, like the IDE settings restore.

        A transcript recorded between the migration and the undo belongs to a
        session this run knows nothing about. Deleting it would destroy work
        the user never asked to roll back.
        """
        later = self.new_entry / "recorded-later.jsonl"
        later.write_text('{"cwd": "/later"}\n', encoding="utf-8")

        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        moved = self.old_entry / "recorded-later.jsonl"
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.read_text(encoding="utf-8"), '{"cwd": "/later"}\n')

    def test_the_recorded_root_is_used_over_the_default(self):
        """The manifest decides where the data goes back, not a guess.

        The configuration file can point claude_root somewhere other than
        ``~/.claude``; restoring into the default would report success while
        leaving the data the user actually has still pointing at the new path.
        """
        elsewhere = self.tmp / "custom-claude"
        (elsewhere / "projects").mkdir(parents=True)
        moved_entry = elsewhere / "projects" / self.new_entry.name
        shutil.move(str(self.new_entry), str(moved_entry))
        shutil.move(str(self.history), str(elsewhere / "history.jsonl"))
        write_manifest(
            self.backup_dir,
            replace(
                read_manifest(self.backup_dir),
                claude_root=str(elsewhere),
                claude_renames=[
                    [str(elsewhere / "projects" / self.old_entry.name), str(moved_entry)]
                ],
            ),
        )

        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        restored = elsewhere / "projects" / self.old_entry.name / self.TRANSCRIPT
        self.assertEqual(restored.read_text(encoding="utf-8"), self.BEFORE)
        self.assertEqual(
            (elsewhere / "history.jsonl").read_text(encoding="utf-8"),
            self.HISTORY_BEFORE,
        )

    def test_an_unrecorded_root_falls_back_to_the_home_directory(self):
        """A manifest that does not say where the data lives has one sane guess.

        ``<home>/.claude`` is the default and therefore where such a run
        necessarily wrote.
        """
        write_manifest(
            self.backup_dir, replace(read_manifest(self.backup_dir), claude_root=None)
        )

        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertEqual(
            (self.old_entry / self.TRANSCRIPT).read_text(encoding="utf-8"),
            self.BEFORE,
        )

    def test_declines_when_the_old_name_is_already_taken(self):
        """Something else at the old name means this is not the state we left.

        ``os.rename`` would replace it without a word, so the rename is skipped
        and reported instead.
        """
        self.old_entry.mkdir()
        (self.old_entry / "someone-elses.jsonl").write_text(
            '{"cwd": "/theirs"}\n', encoding="utf-8"
        )

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(self.new_entry.is_dir())
        self.assertEqual(
            (self.new_entry / self.TRANSCRIPT).read_text(encoding="utf-8"),
            self.AFTER,
        )
        self.assertEqual(
            (self.old_entry / "someone-elses.jsonl").read_text(encoding="utf-8"),
            '{"cwd": "/theirs"}\n',
        )
        self.assertTrue(
            any(
                f"Claude Code entry {self.old_entry} already exists" in line
                for line in lines
            ),
            lines,
        )

    def test_declines_when_the_entry_is_no_longer_there(self):
        shutil.rmtree(self.new_entry)

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(
            any(f"{self.new_entry} not found" in line for line in lines), lines
        )


class TestUndoRestoresTheClaudeRegistry(ClaudeUndoTestCase):
    """The registry is put back whole, because it was rewritten whole.

    A key rename is applied to the parsed document and the document is written
    out again, so nothing short of the original bytes is a reliable reversal.
    """

    def test_the_registry_is_restored_byte_for_byte(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertEqual(
            self.registry.read_bytes(), self.saved_registry.read_bytes()
        )
        self.assertEqual(
            json.loads(self.registry.read_text(encoding="utf-8"))["projects"],
            {str(self.source): {"allowedTools": ["Bash"]}},
        )

    def test_the_permissions_are_restored_too(self):
        """The registry holds every project's tool permissions and MCP servers.

        An undo that put the right bytes back under a wider mode would be a
        quiet security downgrade, so the saved mode comes back with them.
        """
        self.registry.chmod(0o644)
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)

    def test_says_what_it_did(self):
        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertIn(
            f"Restored Claude Code project registry {self.registry}", lines
        )

    def test_the_recorded_path_is_used_and_nothing_is_invented_at_the_default(self):
        """The manifest decides which file is restored, not the default path.

        The configuration file can point the registry somewhere other than
        ``~/.claude.json``. Restoring into the default would report success
        while leaving the file the user actually has still naming the new
        location - and would create a file where there was none.
        """
        elsewhere = self.tmp / "custom" / "registry.json"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(self._registry_text(self.dest), encoding="utf-8")
        self.registry.unlink()
        write_manifest(
            self.backup_dir,
            replace(read_manifest(self.backup_dir), claude_registry=str(elsewhere)),
        )

        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertEqual(
            elsewhere.read_bytes(), self.saved_registry.read_bytes()
        )
        self.assertFalse(self.registry.exists())

    def test_a_missing_saved_copy_is_a_silent_skip(self):
        """No saved file means nothing to put back, and nothing to complain about.

        The rest of the rollback still has to happen: a registry that was never
        copied is not a reason to abandon the directory move or the settings
        restore.
        """
        self.saved_registry.unlink()

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertEqual(
            self.registry.read_text(encoding="utf-8"),
            self._registry_text(self.dest),
        )
        self.assertFalse(any("registry" in line for line in lines), lines)
        self.assertTrue(self.source.is_dir())
        self.assertTrue(self.old_entry.is_dir())

    def test_an_older_manifest_leaves_the_registry_alone(self):
        """Without the recorded path there is nothing this run may overwrite.

        A backup written before the field existed did not rewrite the registry,
        so the file on disk belongs entirely to runs this backup knows nothing
        about - even though a saved copy happens to sit in the backup directory
        here.
        """
        data = json.loads(
            (self.backup_dir / "manifest.json").read_text(encoding="utf-8")
        )
        for key in ("claude_registry", "claude_registry_renames"):
            del data[key]
        (self.backup_dir / "manifest.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertEqual(
            self.registry.read_text(encoding="utf-8"),
            self._registry_text(self.dest),
        )
        self.assertFalse(any("registry" in line for line in lines), lines)
        # The rest of the Claude Code rollback still happened.
        self.assertTrue(self.old_entry.is_dir())


class TestUndoWithoutClaudeFields(ClaudeUndoTestCase):
    def test_an_older_manifest_leaves_the_claude_data_alone(self):
        """A backup written before this feature must still undo, and quietly.

        Its manifest has no claude keys and its backup has no ``claude``
        directory, so there is nothing recorded to reverse. The undo has to
        treat that as "this run touched nothing", not as an error and not as a
        licence to guess.
        """
        # An older backup has neither the manifest keys nor a saved copy of any
        # Claude Code file, so both halves of the fixture are removed.
        write_manifest(self.backup_dir, self.plain_manifest)
        shutil.rmtree(self.backup_dir / "claude")

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        # The directory move and the settings restore still happened.
        self.assertTrue(self.source.is_dir())
        self.assertEqual(
            (self.product / "options" / "recentProjects.xml").read_text(
                encoding="utf-8"
            ),
            "BEFORE",
        )
        self.assertFalse(any("Claude Code" in line for line in lines), lines)
        # Nothing under .claude was renamed or rewritten.
        self.assertTrue(self.new_entry.is_dir())
        self.assertFalse(self.old_entry.exists())
        self.assertEqual(
            (self.new_entry / self.TRANSCRIPT).read_text(encoding="utf-8"),
            self.AFTER,
        )
        self.assertEqual(
            self.history.read_text(encoding="utf-8"), self.HISTORY_AFTER
        )
        # Including the registry, which such a run never rewrote.
        self.assertEqual(
            self.registry.read_text(encoding="utf-8"),
            self._registry_text(self.dest),
        )


class BatchUndoTestCase(unittest.TestCase):
    """A run that moved several directories in one go, and its manifest.

    Every path is inside a throwaway temporary directory standing in for a
    home; the real home directory is never read or written.

    Each fixture project moves from ``<home>/<name>`` to
    ``<home>/Projects/<name>``, which is the shape the tool is used for.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.jetbrains = self.home / "Library" / "Application Support" / "JetBrains"
        self.jetbrains.mkdir(parents=True)
        self.backup_dir = self.tmp / "backups" / "2026-08-29_143005"
        self.backup_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def source_of(self, name: str) -> Path:
        return self.home / name

    def dest_of(self, name: str) -> Path:
        return self.home / "Projects" / name

    def record(self, name: str, status: str = "moved") -> dict:
        """One entry of ``Manifest.moves``, in the shape the tool writes."""
        return {
            "source": str(self.source_of(name)),
            "dest": str(self.dest_of(name)),
            "status": status,
            "rewritten_files": {},
            "claude_renames": [],
            "claude_rewritten_files": {},
            "registry_renames": [],
        }

    def place_at_destination(self, name: str) -> Path:
        """Put a project where a completed move would have left it."""
        dest = self.dest_of(name)
        dest.mkdir(parents=True)
        (dest / "marker.txt").write_text(name, encoding="utf-8")
        return dest

    def place_at_source(self, name: str) -> Path:
        """Put a project where a move that never started would have left it."""
        source = self.source_of(name)
        source.mkdir(parents=True)
        (source / "marker.txt").write_text(name, encoding="utf-8")
        return source

    def write_batch_manifest(self, moves, created_dirs=()) -> None:
        # The top-level source and dest hold the first move's paths, which is
        # what an older reader of this manifest would see.
        write_manifest(
            self.backup_dir,
            Manifest(
                version=1,
                tool_version="0.1.0",
                created_at="2026-08-29T14:30:05",
                home=str(self.home),
                jetbrains_root=str(self.jetbrains),
                source=moves[0]["source"],
                dest=moves[0]["dest"],
                move_status="moved",
                backed_up_products=[],
                rewritten_files={},
                undone_at=None,
                moves=list(moves),
                created_dirs=[str(path) for path in created_dirs],
            ),
        )


class TestUndoOfSeveralMoves(BatchUndoTestCase):
    """Every directory a batch moved comes back, newest reversal first."""

    NAMES = ("Alpha", "Beta", "Gamma")

    def setUp(self):
        super().setUp()
        for name in self.NAMES:
            self.place_at_destination(name)
        self.write_batch_manifest([self.record(name) for name in self.NAMES])

    def test_every_directory_is_moved_back(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        for name in self.NAMES:
            self.assertTrue(self.source_of(name).is_dir(), name)
            self.assertFalse(self.dest_of(name).exists(), name)
            self.assertEqual(
                (self.source_of(name) / "marker.txt").read_text(encoding="utf-8"),
                name,
            )

    def test_the_moves_are_reversed_in_reverse_order(self):
        """The last move made is the first one undone.

        It matters whenever one move's destination sits under another move's
        source: undoing the outer move first would carry the inner directory
        back with it, and the inner reversal would then find nothing where it
        expected something. The reported order is the order the work happened
        in, so it is what pins the behaviour.
        """
        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        moved = [line for line in lines if line.startswith("Moved ")]
        self.assertEqual(
            moved,
            [
                f"Moved {self.dest_of(name)} back to {self.source_of(name)}"
                for name in reversed(self.NAMES)
            ],
        )

    def test_a_nested_destination_survives_the_reversal(self):
        """One move landing inside another move's source is still reversible.

        The batch planner refuses to plan two moves whose sources and
        destinations overlap like this - it never lets ``<home>/Alpha/Inner``
        move under ``<home>/Alpha`` while ``<home>/Alpha`` itself is also
        moving. This test pins the undo's reverse-order reversal on a
        hand-written manifest regardless, because an undo must cope with any
        manifest it is handed, planner-approved or not. Reversing in the same
        order the moves were recorded would move Alpha home first and drag
        Inner along inside it; reversing backwards takes Inner out first,
        which is why the order is not an implementation detail.
        """
        inner_source = self.home / "Alpha" / "Inner"
        inner_dest = self.home / "Elsewhere" / "Inner"
        inner_dest.mkdir(parents=True)
        (inner_dest / "marker.txt").write_text("Inner", encoding="utf-8")
        self.write_batch_manifest(
            [
                self.record("Alpha"),
                {
                    "source": str(inner_source),
                    "dest": str(inner_dest),
                    "status": "moved",
                    "rewritten_files": {},
                    "claude_renames": [],
                    "claude_rewritten_files": {},
                    "registry_renames": [],
                },
            ]
        )

        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(inner_source.is_dir())
        self.assertFalse(inner_dest.exists())
        self.assertEqual(
            (inner_source / "marker.txt").read_text(encoding="utf-8"), "Inner"
        )
        self.assertTrue(self.source_of("Alpha").is_dir())

    def test_the_backup_is_marked_undone_once(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertEqual(read_manifest(self.backup_dir).undone_at, NOW.isoformat())


class TestUndoOfAPartiallyFailedBatch(BatchUndoTestCase):
    """A run that died half way still has to roll back what it managed to do.

    The manifest such a run leaves behind has a completed move, the move that
    failed, and the moves that never started. Two of those directories are at
    their destination and have to come back; the third never left home.
    """

    def setUp(self):
        super().setUp()
        self.place_at_destination("Alpha")
        # The failing move's directory had already left its source when the
        # failure happened - the rewrite that followed it is what broke - so it
        # is sitting at the destination exactly like a completed move's.
        self.place_at_destination("Beta")
        self.place_at_source("Gamma")
        self.write_batch_manifest(
            [
                self.record("Alpha", status="moved"),
                self.record("Beta", status="failed"),
                self.record("Gamma", status="pending"),
            ]
        )

    def test_the_completed_and_failed_moves_both_come_back(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        for name in ("Alpha", "Beta"):
            self.assertTrue(self.source_of(name).is_dir(), name)
            self.assertFalse(self.dest_of(name).exists(), name)

    def test_the_move_that_never_started_is_left_where_it_is(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(self.source_of("Gamma").is_dir())
        self.assertFalse(self.dest_of("Gamma").exists())
        self.assertEqual(
            (self.source_of("Gamma") / "marker.txt").read_text(encoding="utf-8"),
            "Gamma",
        )

    def test_it_says_the_move_was_never_started(self):
        """A move that never ran is expected here, not an anomaly.

        Reporting it as a missing destination would read like damage; saying it
        never started tells the user there is nothing to worry about.
        """
        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertIn(
            f"Move {self.source_of('Gamma')} -> {self.dest_of('Gamma')} "
            "was never started; nothing to reverse.",
            lines,
        )
        self.assertFalse(any("not found" in line for line in lines), lines)

    def test_the_filesystem_decides_not_the_recorded_status(self):
        """A "pending" record whose directory did move is still moved back.

        Ctrl-C between the move and the manifest write leaves exactly that
        record. Trusting the status over what is on disk would strand the
        directory at its new location with nothing left to say so.
        """
        self.write_batch_manifest([self.record("Alpha", status="pending")])

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(self.source_of("Alpha").is_dir())
        self.assertFalse(self.dest_of("Alpha").exists())
        self.assertIn(
            f"Moved {self.dest_of('Alpha')} back to {self.source_of('Alpha')}",
            lines,
        )


class TestUndoRemovesCreatedDirectories(BatchUndoTestCase):
    """Parents the run had to create come back out, if nothing else moved in."""

    def setUp(self):
        super().setUp()
        # <home>/Projects and <home>/Projects/Nested did not exist before the
        # run; it created them to have somewhere to move the project to.
        self.outer = self.home / "Projects"
        self.inner = self.outer / "Nested"
        self.source = self.home / "Alpha"
        self.dest = self.inner / "Alpha"
        self.dest.mkdir(parents=True)
        (self.dest / "marker.txt").write_text("Alpha", encoding="utf-8")
        self.moves = [
            {
                "source": str(self.source),
                "dest": str(self.dest),
                "status": "moved",
                "rewritten_files": {},
                "claude_renames": [],
                "claude_rewritten_files": {},
                "registry_renames": [],
            }
        ]

    def test_empty_created_directories_are_removed_innermost_first(self):
        self.write_batch_manifest(self.moves, created_dirs=[self.outer, self.inner])

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertFalse(self.inner.exists())
        self.assertFalse(self.outer.exists())
        removed = [line for line in lines if line.startswith("Removed ")]
        self.assertEqual(
            removed, [f"Removed {self.inner}", f"Removed {self.outer}"]
        )

    def test_a_directory_someone_else_used_is_left_alone(self):
        """Only empty directories go, and silently so.

        Anything the user put in one of these directories after the migration
        is not this run's to delete, and its presence is not a failure worth
        reporting either.
        """
        self.write_batch_manifest(self.moves, created_dirs=[self.outer, self.inner])
        keeper = self.inner / "somebody-elses-project"
        keeper.mkdir()

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(keeper.is_dir())
        self.assertTrue(self.inner.is_dir())
        self.assertTrue(self.outer.is_dir())
        self.assertFalse(any(line.startswith("Removed ") for line in lines), lines)

    def test_a_directory_that_has_already_gone_is_not_an_error(self):
        self.write_batch_manifest(self.moves, created_dirs=[self.outer, self.inner])
        shutil.rmtree(self.outer)

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertFalse(any(line.startswith("Removed ") for line in lines), lines)
        self.assertEqual(read_manifest(self.backup_dir).undone_at, NOW.isoformat())

    def test_nothing_is_removed_when_the_run_created_nothing(self):
        self.write_batch_manifest(self.moves)

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(self.outer.is_dir())
        self.assertFalse(any(line.startswith("Removed ") for line in lines), lines)


class TestUndoOfAManifestWithoutMoves(BatchUndoTestCase):
    """A backup written before batch runs existed still undoes exactly as before.

    Its manifest has no ``moves`` list at all, so the top-level source and dest
    are the one and only move, and the messages are the ones that route has
    always produced.
    """

    def setUp(self):
        super().setUp()
        self.source = self.home / "Alpha"
        self.dest = self.home / "Projects" / "Alpha"
        self.dest.mkdir(parents=True)
        (self.dest / "marker.txt").write_text("Alpha", encoding="utf-8")
        self.manifest = Manifest(
            version=1,
            tool_version="0.1.0",
            created_at="2026-08-29T14:30:05",
            home=str(self.home),
            jetbrains_root=str(self.jetbrains),
            source=str(self.source),
            dest=str(self.dest),
            move_status="moved",
            backed_up_products=[],
            rewritten_files={},
            undone_at=None,
        )
        write_manifest(self.backup_dir, self.manifest)
        # An older manifest has no such keys at all, rather than empty ones.
        data = json.loads(
            (self.backup_dir / "manifest.json").read_text(encoding="utf-8")
        )
        for key in ("moves", "created_dirs"):
            del data[key]
        (self.backup_dir / "manifest.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def test_the_single_move_is_reversed(self):
        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertIn(f"Moved {self.dest} back to {self.source}", lines)

    def test_the_old_wording_is_unchanged_when_the_source_is_occupied(self):
        self.source.mkdir(parents=True)

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertIn(
            f"Source {self.source} already exists; left the directory alone.", lines
        )

    def test_the_old_wording_is_unchanged_when_the_destination_is_gone(self):
        shutil.rmtree(self.dest)

        lines = undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)

        self.assertIn(
            f"Destination {self.dest} not found; left the directory alone.", lines
        )


if __name__ == "__main__":
    unittest.main()
