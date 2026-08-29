import tempfile
import unittest
from pathlib import Path

from idea_migrate.errors import IdeRunningError
from idea_migrate.mover import assert_no_ide_running, move_directory
from idea_migrate.paths import validate_move

RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOT_RUNNING = "/usr/sbin/cfprefsd\n"


class TestAssertNoIdeRunning(unittest.TestCase):
    def test_passes_when_nothing_is_running(self):
        assert_no_ide_running(NOT_RUNNING)  # must not raise

    def test_raises_and_names_the_ide(self):
        with self.assertRaises(IdeRunningError) as ctx:
            assert_no_ide_running(RUNNING)
        self.assertIn("IntelliJ IDEA", str(ctx.exception))


class TestMoveDirectory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "alpha").mkdir(parents=True)
        (self.source / "alpha" / "file.txt").write_text("hello", encoding="utf-8")
        (self.home / "Projects").mkdir()
        self.dest = self.home / "Projects" / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def test_moves_the_directory_and_its_contents(self):
        spec = validate_move(self.source, self.dest, self.home)
        move_directory(spec)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(
            (self.dest / "alpha" / "file.txt").read_text(encoding="utf-8"), "hello"
        )


if __name__ == "__main__":
    unittest.main()
