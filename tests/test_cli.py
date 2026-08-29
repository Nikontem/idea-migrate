import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from idea_migrate.cli import build_parser, main, run_migration

QUIET = "/usr/sbin/cfprefsd\n"
RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOW = datetime(2026, 8, 29, 14, 30, 5)


class TestParser(unittest.TestCase):
    def test_bare_flags_parse_as_a_migration(self):
        args = build_parser().parse_args(["--source", "/a", "--dest", "/b"])
        self.assertIsNone(args.command)
        self.assertEqual(args.source, "/a")
        self.assertEqual(args.dest, "/b")

    def test_backups_subcommand(self):
        args = build_parser().parse_args(["backups"])
        self.assertEqual(args.command, "backups")

    def test_undo_subcommand_takes_a_directory(self):
        args = build_parser().parse_args(["undo", "/some/backup"])
        self.assertEqual(args.command, "undo")
        self.assertEqual(args.backup_dir, "/some/backup")

    def test_dry_run_flag(self):
        args = build_parser().parse_args(["--source", "/a", "--dest", "/b", "--dry-run"])
        self.assertTrue(args.dry_run)


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "alpha").mkdir(parents=True)
        (self.home / "Projects").mkdir()
        self.dest = self.home / "Projects" / "WebstormProjects"

        product = self.home / "Library" / "Application Support" / "JetBrains" / "WebStorm2026.2"
        (product / "options").mkdir(parents=True)
        (product / "workspace").mkdir(parents=True)
        (product / "options" / "recentProjects.xml").write_text(
            '<application><entry key="$USER_HOME$/WebstormProjects/alpha" /></application>',
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()


class TestRunMigration(MigrationTestCase):
    def test_dry_run_changes_nothing(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--dry-run", "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())

    def test_apply_moves_rewrites_and_backs_up(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())

        config = (
            self.home
            / "Library"
            / "Application Support"
            / "JetBrains"
            / "WebStorm2026.2"
            / "options"
            / "recentProjects.xml"
        )
        self.assertIn(
            "$USER_HOME$/Projects/WebstormProjects/alpha",
            config.read_text(encoding="utf-8"),
        )

        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "manifest.json").is_file())
        self.assertTrue((backups[0] / "undo.sh").is_file())

    def test_output_ends_with_the_undo_command(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertIn("undo.sh", buffer.getvalue().rstrip().splitlines()[-1])

    def test_refuses_while_an_ide_is_running(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_migration(args, self.home, NOW, ps_output=RUNNING)
        self.assertEqual(code, 1)
        self.assertTrue(self.source.is_dir())


class TestMainErrorHandling(unittest.TestCase):
    def test_invalid_path_prints_one_line_and_returns_one(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = main(["--source", "/definitely/not/here", "--dest", "/tmp/x", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
