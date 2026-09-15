import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from idea_migrate.claude_projects import encode_project_path
from idea_migrate.cli import build_parser, main, run_migration
from idea_migrate.errors import JsonIntegrityError, XmlIntegrityError

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


def jsonl(*records: dict) -> str:
    """Render records the way Claude Code writes them: one JSON object a line."""
    return "".join(json.dumps(record) + "\n" for record in records)


class ClaudeMigrationTestCase(MigrationTestCase):
    """A fake home whose ``.claude`` holds data for the directory being moved.

    Everything lives inside a temporary directory: the real ``~/.claude`` is
    never read and never written, by these tests or by the code they drive.

    The fixture deliberately contains three kinds of entry, because the
    interesting behaviour is which ones move and which ones do not:

    * one for the source directory and one for the ``alpha`` subdirectory
      below it - both must be renamed, and the transcripts inside them
      rewritten, including the nested subagent transcript;
    * one for ``WebstormProjectsArchive``, a sibling whose encoded name starts
      with the source's but continues with a letter rather than a dash - it
      must be left completely untouched;
    * one named as if it stood for ``<source>/beta``, a directory that does not
      exist - it must be reported as left alone rather than guessed at.

    The fake home also holds a ``.claude.json`` - Claude Code's per-project
    registry - whose keys are absolute paths rather than encoded names. It
    carries the same three kinds of case, plus one key that repeats the
    ``alpha`` directory with a trailing slash, because Claude Code records a
    working directory exactly as it was given to it and a key must keep its own
    spelling of the tail through the rename.
    """

    SESSION = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        super().setUp()
        self.alpha = self.source / "alpha"
        self.claude_root = self.home / ".claude"
        self.projects = self.claude_root / "projects"
        self.projects.mkdir(parents=True)

        self.source_entry = self.projects / encode_project_path(self.source)
        self.source_entry.mkdir()
        self.source_transcript = self.source_entry / "aaaaaaaa.jsonl"
        self.source_transcript.write_text(
            jsonl(
                {"type": "user", "cwd": str(self.source), "sessionId": "s1"},
                {"type": "assistant", "cwd": str(self.source), "sessionId": "s1"},
            ),
            encoding="utf-8",
        )

        self.alpha_entry = self.projects / encode_project_path(self.alpha)
        self.alpha_entry.mkdir()
        self.alpha_transcript = self.alpha_entry / f"{self.SESSION}.jsonl"
        self.alpha_transcript.write_text(
            jsonl({"type": "user", "cwd": str(self.alpha), "sessionId": self.SESSION}),
            encoding="utf-8",
        )
        subagents = self.alpha_entry / self.SESSION / "subagents"
        subagents.mkdir(parents=True)
        self.subagent_transcript = subagents / "agent-x.jsonl"
        self.subagent_transcript.write_text(
            jsonl(
                {
                    "type": "assistant",
                    "cwd": str(self.alpha),
                    "sessionId": self.SESSION,
                }
            ),
            encoding="utf-8",
        )

        self.archive = self.home / "WebstormProjectsArchive"
        self.archive.mkdir()
        self.archive_entry = self.projects / encode_project_path(self.archive)
        self.archive_entry.mkdir()
        self.archive_transcript = self.archive_entry / "bbbbbbbb.jsonl"
        self.archive_transcript.write_text(
            jsonl({"type": "user", "cwd": str(self.archive), "sessionId": "s3"}),
            encoding="utf-8",
        )

        self.stale_entry = self.projects / (
            encode_project_path(self.source) + "-beta"
        )
        self.stale_entry.mkdir()

        self.unrelated = self.home / "Downloads" / "gamma"
        self.history = self.claude_root / "history.jsonl"
        self.history.write_text(
            jsonl(
                {
                    "display": "run the tests",
                    "pastedContents": {},
                    "project": str(self.alpha),
                    "sessionId": self.SESSION,
                    "timestamp": 1,
                },
                {
                    "display": "somewhere else entirely",
                    "pastedContents": {},
                    "project": str(self.unrelated),
                    "sessionId": "s9",
                    "timestamp": 2,
                },
            ),
            encoding="utf-8",
        )

        # Claude Code's per-project registry. Its keys are the absolute
        # working directories the tool has been started in, and its values are
        # opaque per-project settings that must come through a rename
        # untouched.
        self.registry = self.home / ".claude.json"
        self.registry_projects = {
            str(self.source): {"allowedTools": ["Bash(ls:*)"], "hasTrustDialog": True},
            str(self.alpha): {"allowedTools": ["Read"], "mcpServers": {"x": {}}},
            # The same directory as the line above, spelled with a trailing
            # slash, which is how Claude Code records it when it was started
            # that way. It has to move too, and keep its own trailing slash.
            f"{self.alpha}/": {"allowedTools": ["Edit"]},
            # A sibling whose path starts with the source's but continues with
            # a letter rather than a separator: a different directory, and the
            # exact case a plain prefix match would corrupt.
            str(self.archive): {"allowedTools": ["Write"]},
            str(self.unrelated): {"allowedTools": ["Grep"]},
        }
        self.registry.write_text(
            json.dumps({"projects": self.registry_projects}, indent=2), encoding="utf-8"
        )

    def _registry_keys(self) -> list[str]:
        """The registry's ``projects`` keys, in the order the file lists them."""
        return list(
            json.loads(self.registry.read_text(encoding="utf-8"))["projects"]
        )

    def _run(self, *extra: str, ps_output: str = QUIET):
        """Run a migration and return (exit code, stdout, stderr)."""
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes", *extra]
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_migration(args, self.home, NOW, ps_output=ps_output)
        return code, stdout.getvalue(), stderr.getvalue()

    def _claude_bytes(self) -> dict[str, bytes]:
        """Every file under .claude, keyed by path, for an unchanged check."""
        return {
            str(path): path.read_bytes()
            for path in sorted(self.claude_root.rglob("*"))
            if path.is_file()
        }


class TestClaudeDryRun(ClaudeMigrationTestCase):
    def test_lists_the_rename_pairs_and_the_rewritable_files(self):
        code, output, _ = self._run("--dry-run")
        self.assertEqual(code, 0)

        self.assertIn("Claude Code project entries that would be renamed:", output)
        for old_entry, new_project in (
            (self.source_entry, self.dest),
            (self.alpha_entry, self.dest / "alpha"),
        ):
            new_entry = self.projects / encode_project_path(new_project)
            self.assertIn(f"  {old_entry}\n    ->   {new_entry}\n", output)

        self.assertIn("Claude Code files that would be rewritten:", output)
        # Two references in the source transcript, one in each of the others.
        self.assertIn(f"{self.source_transcript}  (2 references)", output)
        self.assertIn(f"{self.alpha_transcript}  (1 reference)", output)
        self.assertIn(f"{self.subagent_transcript}  (1 reference)", output)
        self.assertIn(f"{self.history}  (1 reference)", output)

        # This fixture's home now also holds a Claude Code registry with three
        # keys to rename, so the summary carries the registry clause as well.
        # The expectation is updated rather than the fixture narrowed: a home
        # with project data almost always has a registry too, and a summary
        # that stopped short of mentioning it would understate the run.
        self.assertRegex(
            output,
            r"Dry run: [1-9]\d* files would be modified, 2 Claude Code entries "
            r"renamed, 4 Claude Code files rewritten and 3 project registry "
            r"keys renamed\. Nothing was written\.",
        )

    def test_names_the_entries_it_would_leave_alone(self):
        """A near miss has to be reported, not silently skipped.

        The entry named as if it stood for ``<source>/beta`` cannot be renamed,
        because no such directory exists and the encoding cannot be reversed to
        find out what it really meant. Saying so before the run is the only way
        a user learns that one transcript will not follow the move.
        """
        code, output, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(
            "Claude Code entries left alone (similar name, but no matching "
            "directory under the source):",
            output,
        )
        self.assertIn(str(self.stale_entry), output)

    def test_names_the_registry_and_counts_its_keys(self):
        code, output, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(
            f"  Claude Code project registry: 3 keys to rename in {self.registry}.",
            output,
        )

    def test_lists_the_registry_key_pairs(self):
        """Each key is shown old above new, like the entry renames.

        A registry key is an absolute path, so the pairs are what let a user
        confirm the tool matched the directories it meant to - including that
        the trailing-slash spelling kept its slash and that the lookalike
        sibling is nowhere in the list.
        """
        code, output, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(
            f"Claude Code project registry keys that would be renamed "
            f"(in {self.registry}):",
            output,
        )
        for old, new in (
            (str(self.source), str(self.dest)),
            (str(self.alpha), f"{self.dest}/alpha"),
            (f"{self.alpha}/", f"{self.dest}/alpha/"),
        ):
            self.assertIn(f"  {old}\n    ->   {new}\n", output)
        self.assertNotIn(f"  {self.archive}\n", output)

    def test_changes_nothing_on_disk(self):
        """A dry run reads every file it reports on, and writes none of them."""
        before = self._claude_bytes()
        registry_before = self.registry.read_bytes()
        code, _, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self._claude_bytes(), before)
        # The registry is read twice in a dry run - once to plan and once to
        # verify the text a real run would write - and written neither time.
        self.assertEqual(self.registry.read_bytes(), registry_before)
        # The entry directories are still under their original names too.
        self.assertTrue(self.source_entry.is_dir())
        self.assertTrue(self.alpha_entry.is_dir())
        self.assertFalse((self.projects / encode_project_path(self.dest)).exists())


class TestClaudeRealRun(ClaudeMigrationTestCase):
    def setUp(self):
        super().setUp()
        self.new_source_entry = self.projects / encode_project_path(self.dest)
        self.new_alpha_entry = self.projects / encode_project_path(
            self.dest / "alpha"
        )

    def test_renames_the_entries(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(self.new_source_entry.is_dir())
        self.assertTrue(self.new_alpha_entry.is_dir())
        self.assertFalse(self.source_entry.exists())
        self.assertFalse(self.alpha_entry.exists())

    def test_rewrites_the_transcripts_including_the_nested_subagent_one(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)

        moved = self.new_alpha_entry / f"{self.SESSION}.jsonl"
        self.assertEqual(
            json.loads(moved.read_text(encoding="utf-8"))["cwd"],
            str(self.dest / "alpha"),
        )
        subagent = (
            self.new_alpha_entry / self.SESSION / "subagents" / "agent-x.jsonl"
        )
        self.assertEqual(
            json.loads(subagent.read_text(encoding="utf-8"))["cwd"],
            str(self.dest / "alpha"),
        )

    def test_rewrites_the_history_but_leaves_unrelated_lines_alone(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        records = [
            json.loads(line)
            for line in self.history.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[0]["project"], str(self.dest / "alpha"))
        self.assertEqual(records[1]["project"], str(self.unrelated))

    def test_the_lookalike_sibling_entry_is_untouched(self):
        """"WebstormProjectsArchive" is a different directory, not a child.

        Its encoded entry name starts with the source's and continues with a
        letter rather than a dash, which is exactly the case a plain prefix
        match would corrupt.
        """
        before = self.archive_transcript.read_bytes()
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(self.archive_entry.is_dir())
        self.assertEqual(self.archive_transcript.read_bytes(), before)

    def test_the_manifest_records_what_was_done(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        manifest = json.loads(
            (backups[0] / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["claude_root"], str(self.claude_root))
        self.assertEqual(
            sorted(manifest["claude_renames"]),
            sorted(
                [
                    [str(self.source_entry), str(self.new_source_entry)],
                    [str(self.alpha_entry), str(self.new_alpha_entry)],
                ]
            ),
        )
        # Recorded at their post-rename paths, which is where undo has to
        # find them.
        rewritten = manifest["claude_rewritten_files"]
        self.assertEqual(rewritten[str(self.history)], 1)
        self.assertEqual(
            rewritten[str(self.new_alpha_entry / f"{self.SESSION}.jsonl")], 1
        )
        self.assertEqual(
            rewritten[str(self.new_source_entry / "aaaaaaaa.jsonl")], 2
        )

    def test_the_rewritten_files_are_copied_into_the_backup(self):
        """Every file the run edits is saved first, at its pre-rename path.

        The entry directories themselves are not copied: a rename is reversed
        by renaming back, so duplicating a transcript tree would be pure cost.
        """
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        backup = list((self.home / "Idea-Migration-Backups").iterdir())[0]
        saved = backup / "claude"

        saved_history = saved / "history.jsonl"
        self.assertTrue(saved_history.is_file())
        self.assertEqual(
            json.loads(saved_history.read_text(encoding="utf-8").splitlines()[0])[
                "project"
            ],
            str(self.alpha),
        )
        self.assertTrue(
            (
                saved
                / "projects"
                / self.alpha_entry.name
                / self.SESSION
                / "subagents"
                / "agent-x.jsonl"
            ).is_file()
        )
        # The untouched sibling's transcript was never copied.
        self.assertFalse((saved / "projects" / self.archive_entry.name).exists())

    def test_the_report_says_what_the_claude_step_did(self):
        code, output, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn(
            "Renamed 2 Claude Code project entries and repaired 5 references "
            "in 4 of their files.",
            output,
        )

    def test_the_relocation_phase_announces_itself(self):
        code, _, progress = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Relocating Claude Code project data...", progress)

    def test_renames_the_registry_keys_and_leaves_their_settings_intact(self):
        """The keys move; the values are copied across without being read.

        A settings object may hold anything Claude Code chooses to put in it,
        including strings that merely look like paths, so the rename must not
        reach inside one.
        """
        code, _, _ = self._run()
        self.assertEqual(code, 0)

        projects = json.loads(self.registry.read_text(encoding="utf-8"))["projects"]
        self.assertEqual(
            projects,
            {
                str(self.dest): self.registry_projects[str(self.source)],
                f"{self.dest}/alpha": self.registry_projects[str(self.alpha)],
                f"{self.dest}/alpha/": self.registry_projects[f"{self.alpha}/"],
                str(self.archive): self.registry_projects[str(self.archive)],
                str(self.unrelated): self.registry_projects[str(self.unrelated)],
            },
        )

    def test_the_lookalike_and_unrelated_registry_keys_are_untouched(self):
        """Neither is inside the directory being moved, so neither moves.

        ``WebstormProjectsArchive`` starts with the source path and continues
        with a letter rather than a separator; ``Downloads/gamma`` has nothing
        to do with the move at all.
        """
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        keys = self._registry_keys()
        self.assertIn(str(self.archive), keys)
        self.assertIn(str(self.unrelated), keys)
        self.assertNotIn(str(self.source), keys)

    def test_the_manifest_records_the_registry_and_its_renames(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        manifest = json.loads(
            (backups[0] / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["claude_registry"], str(self.registry))
        self.assertEqual(
            sorted(manifest["claude_registry_renames"]),
            sorted(
                [
                    [str(self.source), str(self.dest)],
                    [str(self.alpha), f"{self.dest}/alpha"],
                    [f"{self.alpha}/", f"{self.dest}/alpha/"],
                ]
            ),
        )

    def test_the_registry_is_copied_into_the_backup_before_it_is_rewritten(self):
        """The saved copy is the file exactly as it was, byte for byte.

        The registry is rewritten as a whole document rather than patched, so
        the only reversal that can be trusted is the original file.
        """
        original = self.registry.read_bytes()
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        backup = list((self.home / "Idea-Migration-Backups").iterdir())[0]
        saved = backup / "claude-registry.json"
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), original)

    def test_the_report_says_what_the_registry_step_did(self):
        code, output, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn(
            f"  Renamed 3 keys in the Claude Code project registry "
            f"({self.registry}).",
            output,
        )


class TestClaudeFailuresAreClean(ClaudeMigrationTestCase):
    def test_an_entry_already_at_the_destination_name_stops_the_run(self):
        """A name collision must be refused before anything is created.

        ``os.rename`` would replace the existing directory without a word, so
        the collision is detected while planning - which is before the move,
        before the rewrite, and before a backup directory exists to explain.
        """
        occupied = self.projects / encode_project_path(self.dest)
        occupied.mkdir()
        (occupied / "someone-elses.jsonl").write_text("{}\n", encoding="utf-8")

        code, _, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(len(stderr.strip().splitlines()), 1)
        self.assertIn("already exists", stderr)
        self.assertNotIn("Traceback", stderr)

        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertTrue(self.source_entry.is_dir())
        self.assertEqual(
            (occupied / "someone-elses.jsonl").read_text(encoding="utf-8"), "{}\n"
        )
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())

    def test_a_rewrite_that_would_break_json_stops_the_run(self):
        """The JSON integrity check is a MigrateError, so it prints one line.

        It is raised while previewing, which happens before the plan is even
        printed, so nothing has moved and no backup exists.
        """
        with patch(
            "idea_migrate.cli.preview_rewrites",
            side_effect=JsonIntegrityError("would have produced invalid JSON"),
        ):
            code, _, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(
            stderr.strip().splitlines(),
            ["error: would have produced invalid JSON"],
        )
        self.assertNotIn("Traceback", stderr)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())

    def test_a_registry_rewrite_that_would_break_json_stops_the_run(self):
        """The registry's preflight has to fail the same way the XML one does.

        Planning the renames is not enough on its own: the check that the
        rewritten document still holds exactly what was planned only happens
        when the text is produced. A dry run of the rewrite produces it and
        writes nothing, so the failure lands while the directory is still in
        place and no backup exists.
        """
        with patch(
            "idea_migrate.cli.rewrite_registry",
            side_effect=JsonIntegrityError("would have produced invalid JSON"),
        ):
            code, _, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(
            stderr.strip().splitlines(),
            ["error: would have produced invalid JSON"],
        )
        self.assertNotIn("Traceback", stderr)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())

    def test_a_registry_key_already_at_the_destination_stops_the_run(self):
        """A key that is already there would be silently merged away.

        Renaming onto an existing key would discard one project's tool
        permissions and MCP servers with no way to notice, so the collision is
        found while planning - before the move, before any rewrite, and before
        a backup directory exists to explain.
        """
        projects = dict(self.registry_projects)
        projects[str(self.dest)] = {"allowedTools": ["Bash(rm:*)"]}
        original = json.dumps({"projects": projects}, indent=2)
        self.registry.write_text(original, encoding="utf-8")

        code, _, stderr = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(len(stderr.strip().splitlines()), 1)
        self.assertIn("already exists", stderr)
        self.assertNotIn("Traceback", stderr)

        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.registry.read_text(encoding="utf-8"), original)
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())

    def test_a_malformed_registry_warns_and_the_run_continues(self):
        """One unusable file must not abort a migration that is otherwise fine.

        The registry was already broken before this run arrived, so it is
        neither this tool's to fix nor its to blame - but the user has to be
        told, or a skipped registry looks exactly like one with nothing to
        change.
        """
        broken = "{ this is not JSON at all"
        self.registry.write_text(broken, encoding="utf-8")

        with self.assertLogs(
            "idea_migrate.claude_registry", level="WARNING"
        ) as logs:
            code, output, _ = self._run()

        self.assertEqual(code, 0)
        self.assertTrue(any(str(self.registry) in line for line in logs.output))
        # The migration itself went through, and the registry was left exactly
        # as it was found.
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(self.registry.read_text(encoding="utf-8"), broken)
        self.assertNotIn("project registry", output)


class TestMigrationWithoutARegistry(MigrationTestCase):
    """A home with no ``~/.claude.json`` at all, which is entirely ordinary.

    A machine that has never run Claude Code has no registry, and the run has
    to be silent about it rather than reporting on a file that does not exist.
    """

    def _run(self, *extra: str):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes", *extra]
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_the_dry_run_says_nothing_about_a_registry(self):
        code, output, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertNotIn("registry", output)

    def test_the_run_succeeds_and_records_no_registry(self):
        code, output, _ = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn("registry", output)
        self.assertFalse((self.home / ".claude.json").exists())

        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        manifest = json.loads(
            (backups[0] / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(manifest["claude_registry"])
        self.assertEqual(manifest["claude_registry_renames"], [])
        # And nothing was saved that would make an undo try to restore one.
        self.assertFalse((backups[0] / "claude-registry.json").exists())


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
