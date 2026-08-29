import os
import tempfile
import unittest
from pathlib import Path

from idea_migrate.errors import PathValidationError
from idea_migrate.paths import MoveSpec, validate_move


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

    def test_same_directory_is_rejected(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.source, self.home)
        self.assertIn("same directory", str(ctx.exception))

    def test_same_directory_under_a_different_capitalization_is_rejected(self):
        # The filesystem is case-insensitive, so these name one real directory.
        # This must report "same directory", not "already exists".
        other_case = self.home / "webstormprojects"
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, other_case, self.home)
        self.assertIn("same directory", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
