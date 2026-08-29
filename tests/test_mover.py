import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from idea_migrate.errors import IdeRunningError, MoveError
from idea_migrate.mover import assert_no_ide_running, move_directory
from idea_migrate.paths import MoveSpec, validate_move

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


class TestMoveDirectoryCrossDevice(unittest.TestCase):
    """Exercises the copy-then-delete fallback used when the source and the
    destination's parent live on different filesystems (spec.same_device=False).

    MoveSpec is a plain frozen dataclass, so it is constructed directly here
    rather than through validate_move, to reach same_device=False without
    needing two real filesystems.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "alpha").mkdir(parents=True)
        (self.source / "alpha" / "file.txt").write_text("hello", encoding="utf-8")
        (self.source / "link.txt").symlink_to(self.source / "alpha" / "file.txt")
        (self.home / "Projects").mkdir()
        self.dest = self.home / "Projects" / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def _spec(self) -> MoveSpec:
        return MoveSpec(
            source=self.source, dest=self.dest, home=self.home, same_device=False
        )

    def test_copies_then_deletes_the_source_preserving_symlinks(self):
        original_link_target = os.readlink(self.source / "link.txt")

        move_directory(self._spec())

        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(
            (self.dest / "alpha" / "file.txt").read_text(encoding="utf-8"), "hello"
        )
        moved_link = self.dest / "link.txt"
        # symlinks=True copies the link as a link, target text untouched -
        # it still reads exactly what it read before the move.
        self.assertTrue(moved_link.is_symlink())
        self.assertEqual(os.readlink(moved_link), original_link_target)

    def test_verification_failure_raises_and_leaves_source_and_partial_dest(self):
        real_copytree = shutil.copytree

        def _copy_then_drop_a_file(src, dst, **kwargs):
            # shutil.copytree recurses into itself by looking up
            # "shutil.copytree" again for each subdirectory, so the mock
            # below would otherwise be re-entered infinitely. Restoring the
            # real function only for the duration of this one call lets the
            # real recursion run to completion, after which the outer
            # mock.patch block below restores the mock (harmless, since
            # move_directory only calls copytree once at the top level).
            with mock.patch.object(shutil, "copytree", real_copytree):
                real_copytree(src, dst, **kwargs)
            # Simulate a disk-full/interrupted copy: the destination root
            # exists (so a bare is_dir() check would pass) but one file
            # under it never made it across.
            (Path(dst) / "alpha" / "file.txt").unlink()

        with mock.patch("shutil.copytree", side_effect=_copy_then_drop_a_file):
            with self.assertRaises(MoveError):
                move_directory(self._spec())

        # The source must survive intact: this is the only good copy.
        self.assertTrue(self.source.exists())
        self.assertEqual(
            (self.source / "alpha" / "file.txt").read_text(encoding="utf-8"),
            "hello",
        )
        # The partial destination is left in place for inspection, not
        # cleaned up.
        self.assertTrue(self.dest.exists())
        self.assertFalse((self.dest / "alpha" / "file.txt").exists())


if __name__ == "__main__":
    unittest.main()
