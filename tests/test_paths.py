import os
import tempfile
import unittest
from pathlib import Path

from idea_migrate.errors import PathValidationError
from idea_migrate.paths import MoveSpec, validate_move


def _filesystem_is_case_insensitive(directory: Path) -> bool:
    """Ask the filesystem holding ``directory`` whether it ignores letter case.

    A probe directory is created with a mixed-case name; if the all-lowercase
    spelling of that same name also resolves, the filesystem is case-insensitive.
    """
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


class PathTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "a-project").mkdir(parents=True)
        self.dest_parent = self.home / "Projects"
        self.dest_parent.mkdir()
        self.dest = self.dest_parent / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()


class TestValidMove(PathTestCase):
    def test_returns_a_move_spec(self):
        spec = validate_move(self.source, self.dest, self.home)
        self.assertIsInstance(spec, MoveSpec)
        self.assertTrue(os.path.samefile(spec.source, self.source))
        self.assertEqual(spec.dest, self.dest)
        self.assertTrue(spec.same_device)

    def test_expands_tilde_and_relative_input(self):
        spec = validate_move(str(self.source), str(self.dest), self.home)
        self.assertTrue(spec.source.is_absolute())
        self.assertTrue(spec.dest.is_absolute())


class TestRejections(PathTestCase):
    def test_missing_source(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.home / "absent", self.dest, self.home)
        self.assertIn("does not exist", str(ctx.exception))

    def test_source_is_a_file(self):
        a_file = self.home / "a-file.txt"
        a_file.write_text("x", encoding="utf-8")
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(a_file, self.dest, self.home)
        self.assertIn("not a directory", str(ctx.exception))

    def test_source_is_home(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.home, self.dest, self.home)
        self.assertIn("home directory", str(ctx.exception))

    def test_destination_already_exists(self):
        self.dest.mkdir()
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.dest, self.home)
        self.assertIn("already exists", str(ctx.exception))

    def test_destination_parent_missing(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.home / "nope" / "here", self.home)
        self.assertIn("does not exist", str(ctx.exception))

    def test_destination_inside_source_is_rejected(self):
        inside = self.source / "nested" / "target"
        (self.source / "nested").mkdir()
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, inside, self.home)
        self.assertIn("inside", str(ctx.exception))

    def test_destination_inside_source_with_a_missing_parent_reports_the_move(self):
        # The parent directory "nested" does not exist. The useful message is
        # that a directory cannot be moved into itself, not that a parent is
        # missing, so the containment check has to run first.
        inside = self.source / "nested" / "target"
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, inside, self.home)
        self.assertIn("inside", str(ctx.exception))

    def test_same_directory_is_rejected(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.source, self.home)
        self.assertIn("same directory", str(ctx.exception))

    def test_same_directory_under_a_different_capitalization_is_rejected(self):
        # On a case-insensitive filesystem these two spellings name one real
        # directory, and that must be reported as "same directory" rather than
        # "already exists". On a case-sensitive volume they are genuinely two
        # different directories, so there is nothing here to reject and the
        # test would otherwise fail misleadingly.
        if not _filesystem_is_case_insensitive(self.home):
            self.skipTest("filesystem is case-sensitive")
        other_case = self.home / "webstormprojects"
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, other_case, self.home)
        self.assertIn("same directory", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
