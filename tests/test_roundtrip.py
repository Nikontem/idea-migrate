"""End-to-end proof that a migration followed by an undo changes nothing.

The tool's whole safety promise is that you can always get back to where you
started. These tests build a complete synthetic home directory, run a real
migration through the command-line entry point, undo it, and compare a hash of
every file before and after. The backup directory is excluded from the
comparison because the tool deliberately never deletes backups.

This file is also the home for the other recovery properties - the ones about
what survives a failure part-way through - which is why the manifest atomicity
tests live here rather than beside the manifest's own unit tests.
"""

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from idea_migrate.cli import build_parser, run_migration
from idea_migrate.manifest import MANIFEST_NAME, Manifest, read_manifest, write_manifest
from idea_migrate.undo import undo_backup

QUIET = "/usr/sbin/cfprefsd\n"
NOW = datetime(2026, 8, 29, 14, 30, 5)
LATER = datetime(2026, 8, 30, 9, 0, 0)

RECENT_PROJECTS = """\
<application>
  <component name="RecentProjectsManager">
    <option name="additionalInfo">
      <map>
        <entry key="$USER_HOME$/WebstormProjects/alpha">
          <value><RecentProjectMetaInfo projectWorkspaceId="AAA" /></value>
        </entry>
        <entry key="$USER_HOME$/WebstormProjectsArchive/beta">
          <value><RecentProjectMetaInfo projectWorkspaceId="BBB" /></value>
        </entry>
        <entry key="$USER_HOME$/Downloads/gamma">
          <value><RecentProjectMetaInfo projectWorkspaceId="CCC" /></value>
        </entry>
      </map>
    </option>
  </component>
</application>
"""

WORKSPACE = (
    '<project><component name="X" path="$USER_HOME$/WebstormProjects/alpha" />'
    "</project>\n"
)


def snapshot(root: Path, skip: Path) -> dict[str, str]:
    """Describe every entry under ``root``, ignoring anything under ``skip``.

    Each entry is recorded with its permission bits as well as its content, so
    a restore that puts the right bytes back under the wrong mode still shows
    up as a difference. Symbolic links are recorded by what they point at
    rather than by following them, which keeps a link and a real file with the
    same contents from looking identical - and, more importantly, stops an
    entry that is neither a regular file nor a directory (a broken link, say)
    from being silently left out of a comparison that claims to cover
    everything.
    """
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if skip in path.parents or path == skip:
            continue
        key = str(path.relative_to(root))
        mode = oct(stat.S_IMODE(path.lstat().st_mode))
        if path.is_symlink():
            body = f"<symlink> -> {os.readlink(path)}"
        elif path.is_dir():
            body = "<dir>"
        elif path.is_file():
            body = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            body = "<other>"
        digest[key] = f"{mode} {body}"
    return digest


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()

        for project in ("alpha", "beta"):
            (self.home / "WebstormProjects" / project / ".idea").mkdir(parents=True)
            (self.home / "WebstormProjects" / project / "index.js").write_text(
                f"// {project}\n", encoding="utf-8"
            )
        # A real project tree is not just plain files. An executable script and
        # a symbolic link are here so the comparison has something to say about
        # permission bits and link targets, not only about file contents.
        script = self.home / "WebstormProjects" / "alpha" / "build.sh"
        script.write_text("#!/bin/sh\necho build\n", encoding="utf-8")
        script.chmod(0o755)
        (self.home / "WebstormProjects" / "alpha" / "entry.js").symlink_to("index.js")

        (self.home / "WebstormProjectsArchive" / "beta").mkdir(parents=True)
        (self.home / "Downloads" / "gamma").mkdir(parents=True)
        (self.home / "Projects").mkdir()

        jetbrains = self.home / "Library" / "Application Support" / "JetBrains"
        for name in ("WebStorm2026.2", "IntelliJIdea2026.2", "PyCharm2025.3"):
            product = jetbrains / name
            (product / "options").mkdir(parents=True)
            (product / "workspace").mkdir(parents=True)
            (product / "plugins").mkdir(parents=True)
            (product / "options" / "recentProjects.xml").write_text(
                RECENT_PROJECTS, encoding="utf-8"
            )
            (product / "workspace" / "AAA.xml").write_text(WORKSPACE, encoding="utf-8")
            (product / "plugins" / "big.jar").write_text("x" * 50, encoding="utf-8")

        # One configuration file is deliberately owner-only. Rewriting it has
        # to leave that permission alone, and undoing has to put it back, so a
        # migration can never quietly widen who can read a user's settings.
        (jetbrains / "PyCharm2025.3" / "options" / "recentProjects.xml").chmod(0o600)

        self.jetbrains = jetbrains
        self.backup_root = self.home / "Idea-Migration-Backups"
        self.source = self.home / "WebstormProjects"
        self.dest = self.home / "Projects" / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def test_migrate_then_undo_restores_everything(self):
        before = snapshot(self.home, self.backup_root)

        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        with redirect_stdout(io.StringIO()):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)

        # The migration really happened.
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        config = self.jetbrains / "WebStorm2026.2" / "options" / "recentProjects.xml"
        text = config.read_text(encoding="utf-8")
        self.assertIn("$USER_HOME$/Projects/WebstormProjects/alpha", text)
        # The near-miss sibling and the unrelated path were left alone.
        self.assertIn("$USER_HOME$/WebstormProjectsArchive/beta", text)
        self.assertIn("$USER_HOME$/Downloads/gamma", text)

        backups = sorted(self.backup_root.iterdir())
        self.assertEqual(len(backups), 1)

        with redirect_stdout(io.StringIO()):
            undo_backup(backups[0], self.jetbrains, LATER, ps_output=QUIET)

        after = snapshot(self.home, self.backup_root)
        self.assertEqual(before, after)

    def test_backup_survives_the_undo(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        with redirect_stdout(io.StringIO()):
            run_migration(args, self.home, NOW, ps_output=QUIET)
        backup = sorted(self.backup_root.iterdir())[0]
        with redirect_stdout(io.StringIO()):
            undo_backup(backup, self.jetbrains, LATER, ps_output=QUIET)
        self.assertTrue(backup.is_dir())
        self.assertTrue((backup / "manifest.json").is_file())
        self.assertTrue((backup / "undo.sh").is_file())

    def test_all_products_are_repaired_not_just_one(self):
        """Every installed product, and both of the directories each one keeps.

        A product stores paths in two places: ``options/``, which holds the
        recent-projects list, and ``workspace/``, which holds per-project
        state. Both are checked here, for all three products, because a
        migration that repaired only the first product it found - or only the
        options directory - would leave the rest of the IDEs pointing at a
        directory that no longer exists.
        """
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        with redirect_stdout(io.StringIO()):
            run_migration(args, self.home, NOW, ps_output=QUIET)

        new_path = "$USER_HOME$/Projects/WebstormProjects/alpha"
        old_path = "$USER_HOME$/WebstormProjects/alpha"
        for name in ("WebStorm2026.2", "IntelliJIdea2026.2", "PyCharm2025.3"):
            for relative in ("options/recentProjects.xml", "workspace/AAA.xml"):
                config = self.jetbrains / name / relative
                text = config.read_text(encoding="utf-8")
                self.assertIn(new_path, text, f"{name}/{relative} was not repaired")
                self.assertNotIn(
                    old_path,
                    text,
                    f"{name}/{relative} still points at the old location",
                )


def sample_manifest() -> Manifest:
    return Manifest(
        version=1,
        tool_version="0.1.0",
        created_at="2026-08-29T14:30:05",
        home="/Users/tester",
        source="/Users/tester/WebstormProjects",
        dest="/Users/tester/Projects/WebstormProjects",
        move_status="moved",
        backed_up_products=["WebStorm2026.2"],
        rewritten_files={"/x/options/recentProjects.xml": 3},
        undone_at=None,
    )


class TestManifestSurvivesAFailedWrite(unittest.TestCase):
    """The manifest must never be left half-written.

    ``write_manifest`` writes to a temporary file, flushes it to disk, and only
    then moves it into position with ``os.replace``, which either fully
    succeeds or does nothing. That matters because the manifest is the only
    record of what a run did: if a crash part-way through a write could leave a
    truncated ``manifest.json``, both ``undo`` and the backups listing would
    fail on a backup whose saved settings files were otherwise perfectly
    intact - the tool would break recovery at exactly the moment recovery is
    needed.

    These tests simulate the crash by making the final move fail.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.backup_dir = Path(self._tmp.name)
        self.original = sample_manifest()
        write_manifest(self.backup_dir, self.original)

        self.replacement = replace(
            self.original,
            dest="/Users/tester/Projects/SomewhereElse",
            move_status="copied",
            rewritten_files={"/y/options/recentProjects.xml": 99},
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _write_with_failing_replace(self):
        """Attempt a second write whose final ``os.replace`` fails."""
        with patch(
            "idea_migrate.manifest.os.replace", side_effect=OSError("simulated crash")
        ):
            write_manifest(self.backup_dir, self.replacement)

    def test_the_failure_is_reported_rather_than_swallowed(self):
        with self.assertRaises(OSError):
            self._write_with_failing_replace()

    def test_the_previous_manifest_is_still_intact(self):
        with self.assertRaises(OSError):
            self._write_with_failing_replace()

        target = self.backup_dir / MANIFEST_NAME
        self.assertTrue(target.is_file(), "the manifest was removed by a failed write")

        # It still parses, and it still holds the original run's details -
        # not the half-written replacement, and not a truncated fragment.
        raw = target.read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw)["dest"], self.original.dest)
        self.assertEqual(read_manifest(self.backup_dir), self.original)

    def test_the_manifest_is_still_usable_for_a_later_successful_write(self):
        with self.assertRaises(OSError):
            self._write_with_failing_replace()

        # Recovery is not a dead end: once the underlying problem clears, the
        # next write goes through normally.
        write_manifest(self.backup_dir, self.replacement)
        self.assertEqual(read_manifest(self.backup_dir), self.replacement)

    def test_no_stray_temporary_file_is_left_behind(self):
        """A failed write cleans up after itself.

        The manifest survives a failed write either way, so recovery works
        regardless - but the tool never deletes a backup directory, so a
        temporary file left there would stay for good, dead weight that
        inflates the size shown by ``idea-migrate backups``. Nothing but the
        manifest should remain.
        """
        with self.assertRaises(OSError):
            self._write_with_failing_replace()

        leftovers = sorted(
            entry.name
            for entry in self.backup_dir.iterdir()
            if entry.name != MANIFEST_NAME
        )
        self.assertEqual(leftovers, [], f"temporary files left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
