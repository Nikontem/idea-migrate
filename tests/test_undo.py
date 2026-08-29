import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
