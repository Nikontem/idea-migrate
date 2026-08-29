import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from idea_migrate.cli import build_parser, main, run_migration
from idea_migrate.errors import XmlIntegrityError

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
        self.config_file = product / "options" / "recentProjects.xml"
        self.config_file.write_text(
            '<application><entry key="$USER_HOME$/WebstormProjects/alpha" /></application>',
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()


class TestRunMigration(MigrationTestCase):
    def test_dry_run_changes_nothing(self):
        original_bytes = self.config_file.read_bytes()
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

        # The one file rewrite_products(dry_run=True) actually opens must be
        # byte-for-byte unchanged - not just "the destination doesn't exist".
        self.assertEqual(self.config_file.read_bytes(), original_bytes)

        output = buffer.getvalue()
        self.assertRegex(output, r"Dry run: [1-9]\d* files would be modified")

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
        manifest_path = backups[0] / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        self.assertTrue((backups[0] / "undo.sh").is_file())

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["move_status"], "moved")
        self.assertTrue(manifest["rewritten_files"])

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

    def test_failure_after_the_move_still_reports_the_undo_command(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        stderr = io.StringIO()
        with (
            patch(
                "idea_migrate.cli.rewrite_products",
                side_effect=XmlIntegrityError("would have produced invalid XML"),
            ),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 1)

        # The move already happened - the source is gone - so the failure
        # message must point at the recoverable backup, not just say "error".
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())

        output = stderr.getvalue()
        self.assertIn("undo.sh", output)
        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), output)


class TestMainErrorHandling(unittest.TestCase):
    def test_invalid_path_prints_one_line_and_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            dest = home / "somewhere" / "x"
            stderr = io.StringIO()
            with (
                patch("idea_migrate.cli.Path.home", return_value=home),
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
            ):
                code = main(["--source", "/definitely/not/here", "--dest", str(dest), "--yes"])
            self.assertEqual(code, 1)
            self.assertIn("does not exist", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_dry_run_is_rejected_on_subcommands(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = main(["--dry-run", "backups"])
        self.assertEqual(code, 1)
        self.assertIn("dry-run", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
