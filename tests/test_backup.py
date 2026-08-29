import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from idea_migrate.backup import (
    UNDO_SCRIPT_NAME,
    back_up_products,
    new_backup_dir,
    write_undo_script,
)


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.jetbrains = self.tmp / "JetBrains"
        self.products = []
        for name in ("IntelliJIdea2026.2", "PyCharm2026.2"):
            product = self.jetbrains / name
            (product / "options").mkdir(parents=True)
            (product / "workspace").mkdir(parents=True)
            (product / "plugins").mkdir(parents=True)
            (product / "options" / "recentProjects.xml").write_text(
                "<application />", encoding="utf-8"
            )
            (product / "workspace" / "AAA.xml").write_text(
                "<project />", encoding="utf-8"
            )
            (product / "plugins" / "huge.jar").write_text("x" * 100, encoding="utf-8")
            self.products.append(product)
        self.backup_root = self.tmp / "Idea-Migration-Backups"

    def tearDown(self):
        self._tmp.cleanup()


class TestNewBackupDir(BackupTestCase):
    def test_creates_a_timestamped_directory(self):
        when = datetime(2026, 8, 29, 14, 30, 5)
        created = new_backup_dir(self.backup_root, when)
        self.assertTrue(created.is_dir())
        self.assertEqual(created.name, "2026-08-29_143005")
        self.assertEqual(created.parent, self.backup_root)

    def test_creates_the_backup_root_if_absent(self):
        self.assertFalse(self.backup_root.exists())
        new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        self.assertTrue(self.backup_root.is_dir())

    def test_second_run_in_the_same_second_gets_a_distinct_directory(self):
        when = datetime(2026, 8, 29, 14, 30, 5)
        first = new_backup_dir(self.backup_root, when)
        second = new_backup_dir(self.backup_root, when)
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_dir())


class TestBackUpProducts(BackupTestCase):
    def test_copies_options_and_workspace_only(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        names = back_up_products(self.products, backup_dir)
        self.assertEqual(names, ["IntelliJIdea2026.2", "PyCharm2026.2"])
        config = backup_dir / "config"
        self.assertTrue((config / "IntelliJIdea2026.2" / "options" / "recentProjects.xml").is_file())
        self.assertTrue((config / "IntelliJIdea2026.2" / "workspace" / "AAA.xml").is_file())
        self.assertFalse((config / "IntelliJIdea2026.2" / "plugins").exists())

    def test_file_contents_are_preserved(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        back_up_products(self.products, backup_dir)
        copied = (
            backup_dir / "config" / "PyCharm2026.2" / "options" / "recentProjects.xml"
        )
        self.assertEqual(copied.read_text(encoding="utf-8"), "<application />")


class TestUndoScript(BackupTestCase):
    def test_script_is_written_and_executable(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        self.assertEqual(script.name, UNDO_SCRIPT_NAME)
        self.assertTrue(script.is_file())
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)

    def test_script_passes_a_syntax_check(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_accepts_a_path_argument(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        text = write_undo_script(backup_dir).read_text(encoding="utf-8")
        self.assertIn('${1:-', text)

    def test_script_refuses_without_a_manifest(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest", (result.stderr + result.stdout).lower())


if __name__ == "__main__":
    unittest.main()
