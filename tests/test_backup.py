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
from idea_migrate.manifest import Manifest, read_manifest, write_manifest


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

    def test_script_refuses_without_a_manifest(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest", (result.stderr + result.stdout).lower())


class TestUndoScriptBehavior(unittest.TestCase):
    """Runs the generated undo.sh against a real manifest and checks the effect.

    Each test builds its own fake $HOME, moved-project source/dest pair, and
    backup directory (manifest + config snapshot), then executes undo.sh as a
    subprocess and asserts on the filesystem afterward.
    """

    PRODUCT = "IntelliJIdea2026.2"
    PRE_MIGRATION_TEXT = "PRE-MIGRATION"
    POST_MIGRATION_TEXT = "POST-MIGRATION"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _build_scenario(self, *, create_dest=True, create_source=False):
        home = self.tmp / "home"
        options_dir = (
            home
            / "Library"
            / "Application Support"
            / "JetBrains"
            / self.PRODUCT
            / "options"
        )
        options_dir.mkdir(parents=True)
        options_file = options_dir / "settings.xml"
        options_file.write_text(self.POST_MIGRATION_TEXT, encoding="utf-8")

        source = self.tmp / "OldLocation" / "MyProject"
        dest = self.tmp / "NewLocation" / "MyProject"
        # The parent directory of source (e.g. "~/Projects") survives a real
        # move even though the project itself does not; only the leaf
        # directory was relocated. Pre-create it so a plain `mv` back to
        # source has somewhere to land.
        source.parent.mkdir(parents=True, exist_ok=True)
        if create_dest:
            dest.mkdir(parents=True)
            (dest / "marker.txt").write_text("project file", encoding="utf-8")
        if create_source:
            source.mkdir(parents=True)
            (source / "marker.txt").write_text("original project file", encoding="utf-8")

        backup_root = self.tmp / "Idea-Migration-Backups"
        backup_dir = new_backup_dir(backup_root, datetime(2026, 8, 29, 14, 30, 5))
        config_options = backup_dir / "config" / self.PRODUCT / "options"
        config_options.mkdir(parents=True)
        (config_options / "settings.xml").write_text(
            self.PRE_MIGRATION_TEXT, encoding="utf-8"
        )

        manifest = Manifest(
            version=1,
            tool_version="test",
            created_at="2026-08-29T14:30:05",
            home=str(home),
            source=str(source),
            dest=str(dest),
            move_status="moved",
            backed_up_products=[self.PRODUCT],
            rewritten_files={},
            undone_at=None,
        )
        write_manifest(backup_dir, manifest)
        script = write_undo_script(backup_dir)

        return {
            "home": home,
            "source": source,
            "dest": dest,
            "backup_dir": backup_dir,
            "script": script,
            "options_file": options_file,
        }

    def _stub_pgrep(self, exit_code):
        stub_dir = self.tmp / f"stub_bin_{exit_code}"
        stub_dir.mkdir(exist_ok=True)
        pgrep = stub_dir / "pgrep"
        pgrep.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        pgrep.chmod(pgrep.stat().st_mode | stat.S_IXUSR)
        return stub_dir

    def _run_script(self, script, stub_dir, arg=None, cwd=None):
        args = ["bash", str(script)]
        if arg is not None:
            args.append(str(arg))
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd)

    def test_happy_path_moves_dest_and_restores_config(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_pgrep(exit_code=1)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(scenario["source"].is_dir())
        self.assertFalse(scenario["dest"].exists())
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.PRE_MIGRATION_TEXT,
        )

    def test_undone_at_is_stamped(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_pgrep(exit_code=1)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = read_manifest(scenario["backup_dir"])
        self.assertIsNotNone(manifest.undone_at)

    def test_refuses_to_undo_twice(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_pgrep(exit_code=1)

        first = self._run_script(scenario["script"], stub)
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self._run_script(scenario["script"], stub)

        self.assertNotEqual(second.returncode, 0)
        # Nothing new should have moved: source (moved back on the first run)
        # is still there, and dest was not somehow recreated.
        self.assertTrue(scenario["source"].is_dir())
        self.assertFalse(scenario["dest"].exists())

    def test_declines_when_source_already_exists(self):
        scenario = self._build_scenario(create_dest=True, create_source=True)
        stub = self._stub_pgrep(exit_code=1)

        self._run_script(scenario["script"], stub)

        self.assertTrue(scenario["source"].is_dir())
        self.assertTrue(scenario["dest"].is_dir())
        self.assertEqual(
            (scenario["source"] / "marker.txt").read_text(encoding="utf-8"),
            "original project file",
        )
        self.assertEqual(
            (scenario["dest"] / "marker.txt").read_text(encoding="utf-8"),
            "project file",
        )

    def test_declines_when_destination_is_missing(self):
        scenario = self._build_scenario(create_dest=False, create_source=False)
        stub = self._stub_pgrep(exit_code=1)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(scenario["source"].exists())
        self.assertFalse(scenario["dest"].exists())
        self.assertTrue(
            (scenario["backup_dir"] / "config" / self.PRODUCT / "options").is_dir()
        )

    def test_path_argument_works_from_a_different_working_directory(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_pgrep(exit_code=1)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()

        result = self._run_script(
            scenario["script"], stub, arg=scenario["backup_dir"], cwd=elsewhere
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(scenario["source"].is_dir())
        self.assertFalse(scenario["dest"].exists())
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.PRE_MIGRATION_TEXT,
        )

    def test_refuses_when_an_ide_appears_to_be_running(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_pgrep(exit_code=0)

        result = self._run_script(scenario["script"], stub)

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(scenario["dest"].is_dir())
        self.assertFalse(scenario["source"].exists())
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.POST_MIGRATION_TEXT,
        )


if __name__ == "__main__":
    unittest.main()
