import io
import json
import re
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


# run_migration calls rewrite_products twice: once as a dry-run preflight
# before the move, to reject a destination that would break the XML while the
# directory is still in place, and once for real afterwards. Tests that need
# the *post-move* call to fail therefore let the first call succeed by giving
# it an empty result, and raise on the second.
PREFLIGHT_OK: dict[str, int] = {}


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
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
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

    def test_dry_run_names_the_files_that_would_change(self):
        """The plan has to say which configuration files it would touch.

        A count alone gives the user nothing to check. Naming the files and
        the number of references in each is what makes a dry run reviewable
        before a real run is authorised.
        """
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--dry-run", "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn(str(self.config_file), output)
        self.assertRegex(output, re.escape(str(self.config_file)) + r"\s+\(1 reference\)")

    def test_destination_that_would_break_the_xml_fails_before_anything_moves(self):
        """An XML-hostile destination must be refused before the move, not after.

        A destination whose name contains "&" cannot be written into an XML
        attribute as it stands, so the rewrite would refuse it. Discovering
        that only after the directory has been copied means the user waits out
        a multi-gigabyte move to be told it was never going to work, and is
        then left with the directory sitting in its new location while the
        IDEs still point at the old one.
        """
        hostile_dest = self.home / "Projects" / "R&D"
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(hostile_dest), "--yes"]
        )
        original_bytes = self.config_file.read_bytes()
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)

        self.assertEqual(code, 1)
        self.assertIn("invalid XML", stderr.getvalue())
        # Nothing moved, and no configuration file was written.
        self.assertTrue(self.source.is_dir())
        self.assertFalse(hostile_dest.exists())
        self.assertEqual(self.config_file.read_bytes(), original_bytes)
        # And no backup either. The check runs before anything is created, so
        # a run that never started leaves nothing behind - otherwise a
        # "pending" backup would sit in the backups listing offering undo
        # commands for a migration that did not happen, in a directory the
        # tool promises never to clean up.
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())
        # No recovery advice, because there is nothing to recover from.
        self.assertNotIn("failed partway through", stderr.getvalue())

    def test_apply_moves_rewrites_and_backs_up(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
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

    def test_each_long_phase_announces_itself_on_stderr(self):
        """A long run must not be silent, and must not pollute the report.

        Backing up a dozen products, moving the directory, repairing the
        configuration and scanning the moved tree all take time with nothing
        to show for it, and the scan runs immediately after the riskiest step.
        Each phase says what it is starting, on stderr, so redirecting the
        report on stdout still yields only the report.
        """
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)

        progress = stderr.getvalue()
        self.assertIn("Backing up", progress)
        self.assertIn("Moving", progress)
        self.assertIn("Repairing", progress)
        self.assertIn("Scanning", progress)

        # The report itself is unaffected: none of the progress lines leak
        # into stdout, which a user may be piping.
        report = stdout.getvalue()
        self.assertNotIn("Backing up", report)
        self.assertNotIn("Scanning", report)
        self.assertIn("Migration complete", report)

    def test_output_ends_with_the_undo_command(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertIn("undo.sh", buffer.getvalue().rstrip().splitlines()[-1])

    def test_refuses_while_an_ide_is_running(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
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
                side_effect=[PREFLIGHT_OK, XmlIntegrityError("would have produced invalid XML")],
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

    def test_keyboard_interrupt_after_the_move_still_reports_the_undo_command(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        stderr = io.StringIO()
        with (
            patch(
                "idea_migrate.cli.rewrite_products",
                side_effect=[PREFLIGHT_OK, KeyboardInterrupt()],
            ),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_migration(args, self.home, NOW, ps_output=QUIET)

        # The recovery message must be driven by whether the move actually
        # happened, not by the exception's type - Ctrl-C is neither a
        # MigrateError nor an OSError.
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        output = stderr.getvalue()
        self.assertIn("undo.sh", output)
        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), output)

    def test_unanticipated_exception_after_the_move_still_reports_the_undo_command(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        stderr = io.StringIO()
        with (
            patch(
                "idea_migrate.cli.rewrite_products",
                side_effect=[PREFLIGHT_OK, RuntimeError("unexpected bug")],
            ),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(RuntimeError):
                run_migration(args, self.home, NOW, ps_output=QUIET)

        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        output = stderr.getvalue()
        self.assertIn("undo.sh", output)
        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), output)


class TestUndoUsesTheRecordedSettingsRoot(unittest.TestCase):
    """The undo command and undo.sh must agree on where to restore.

    Both read the settings directory from the manifest, so a user who
    migrated with a --config pointing jetbrains_root somewhere non-standard
    does not have to remember that flag to undo. Before this, the script read
    the recorded value while the command used whatever configuration happened
    to be in effect, so the two disagreed and a forgotten --config restored
    into the wrong directory while reporting success.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.custom_root = self.home / "custom-jetbrains"
        self.product = self.custom_root / "WebStorm2026.2"
        (self.product / "options").mkdir(parents=True)
        (self.product / "options" / "recentProjects.xml").write_text(
            '<application><entry key="$USER_HOME$/WebstormProjects/alpha" /></application>',
            encoding="utf-8",
        )

        self.source = self.home / "WebstormProjects"
        (self.source / "alpha").mkdir(parents=True)
        (self.home / "Projects").mkdir()
        self.dest = self.home / "Projects" / "WebstormProjects"

        self.config_path = self.home / "config.toml"
        self.config_path.write_text(
            f'jetbrains_root = "{self.custom_root}"\n', encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _migrate(self):
        args = build_parser().parse_args(
            [
                "--config", str(self.config_path),
                "--source", str(self.source),
                "--dest", str(self.dest),
                "--yes",
            ]
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run_migration(args, self.home, NOW, ps_output=QUIET), 0)
        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        self.assertEqual(len(backups), 1)
        return backups[0]

    def test_undo_without_the_config_flag_still_restores_the_right_directory(self):
        backup_dir = self._migrate()
        config_file = self.product / "options" / "recentProjects.xml"
        self.assertIn("Projects/WebstormProjects", config_file.read_text(encoding="utf-8"))

        # Undo, deliberately without --config: the manifest is what decides.
        with (
            patch("idea_migrate.cli.Path.home", return_value=self.home),
            patch("idea_migrate.undo.assert_no_ide_running"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = main(["undo", str(backup_dir)])

        self.assertEqual(code, 0)
        self.assertIn(
            "$USER_HOME$/WebstormProjects/alpha",
            config_file.read_text(encoding="utf-8"),
        )
        # Nothing was invented at the default location.
        self.assertFalse(
            (self.home / "Library" / "Application Support" / "JetBrains").exists()
        )

    def test_an_older_manifest_falls_back_to_the_configured_directory(self):
        """With nothing recorded, the configuration is the best guess."""
        backup_dir = self._migrate()
        manifest_path = backup_dir / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        del data["jetbrains_root"]
        manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        with (
            patch("idea_migrate.cli.Path.home", return_value=self.home),
            patch("idea_migrate.undo.assert_no_ide_running"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = main(["--config", str(self.config_path), "undo", str(backup_dir)])

        self.assertEqual(code, 0)
        self.assertIn(
            "$USER_HOME$/WebstormProjects/alpha",
            (self.product / "options" / "recentProjects.xml").read_text(
                encoding="utf-8"
            ),
        )


class TestPromptForPathNonInteractive(unittest.TestCase):
    def test_no_stdin_raises_a_clean_message_instead_of_a_traceback(self):
        stderr = io.StringIO()
        with (
            patch("builtins.input", side_effect=EOFError()),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            code = main(["--dest", "/tmp/somewhere", "--yes"])
        self.assertEqual(code, 1)
        output = stderr.getvalue()
        self.assertIn("--source", output)
        self.assertNotIn("Traceback", output)


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
