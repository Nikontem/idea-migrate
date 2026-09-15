import itertools
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from idea_migrate.backup import (
    REGISTRY_BACKUP_NAME,
    UNDO_SCRIPT_NAME,
    back_up_claude_files,
    back_up_products,
    back_up_registry,
    new_backup_dir,
    write_undo_script,
)
from idea_migrate.claude_projects import encode_project_path
from idea_migrate.ide_caches import (
    cache_suffix,
    plan_cache_renames,
    rename_caches,
    rewrite_caches,
)
from idea_migrate.rewrite import prefix_variants
from idea_migrate.errors import BackupError
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


class TestBackUpClaudeFiles(BackupTestCase):
    """Only the Claude Code files a run will rewrite are copied, and as they are.

    Everything happens inside a temporary directory standing in for a home;
    the real ``~/.claude`` is never touched.
    """

    def setUp(self):
        super().setUp()
        self.claude_root = self.tmp / ".claude"
        self.entry = self.claude_root / "projects" / "-tmp-MyProject"
        (self.entry / "session" / "subagents").mkdir(parents=True)
        self.history = self.claude_root / "history.jsonl"
        self.history.write_text('{"project": "/old"}\n', encoding="utf-8")
        self.transcript = self.entry / "session" / "subagents" / "agent-x.jsonl"
        self.transcript.write_text('{"cwd": "/old"}\n', encoding="utf-8")
        self.untouched = self.entry / "not-changing.jsonl"
        self.untouched.write_text('{"cwd": "/elsewhere"}\n', encoding="utf-8")
        self.backup_dir = new_backup_dir(
            self.backup_root, datetime(2026, 8, 29, 14, 30, 5)
        )

    def test_copies_the_given_files_under_their_relative_paths(self):
        copied = back_up_claude_files(
            self.claude_root, [self.history, self.transcript], self.backup_dir
        )
        self.assertEqual(
            copied,
            ["history.jsonl", "projects/-tmp-MyProject/session/subagents/agent-x.jsonl"],
        )
        saved = self.backup_dir / "claude"
        self.assertEqual(
            (saved / "history.jsonl").read_text(encoding="utf-8"),
            '{"project": "/old"}\n',
        )
        self.assertTrue(
            (
                saved
                / "projects"
                / "-tmp-MyProject"
                / "session"
                / "subagents"
                / "agent-x.jsonl"
            ).is_file()
        )

    def test_files_that_will_not_change_are_not_copied(self):
        """The backup is sized to what the run will edit, nothing more.

        A transcript tree can run to hundreds of megabytes and the rename is
        reversed by renaming back, so copying anything the rewrite will not
        touch buys nothing.
        """
        back_up_claude_files(self.claude_root, [self.history], self.backup_dir)
        self.assertFalse((self.backup_dir / "claude" / "projects").exists())

    def test_permissions_are_preserved(self):
        """An owner-only transcript must not come back readable by everyone."""
        self.transcript.chmod(0o600)
        back_up_claude_files(self.claude_root, [self.transcript], self.backup_dir)
        copied = (
            self.backup_dir
            / "claude"
            / "projects"
            / "-tmp-MyProject"
            / "session"
            / "subagents"
            / "agent-x.jsonl"
        )
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)

    def test_nothing_to_copy_creates_nothing(self):
        self.assertEqual(
            back_up_claude_files(self.claude_root, [], self.backup_dir), []
        )
        self.assertFalse((self.backup_dir / "claude").exists())

    def test_an_unreadable_file_is_reported_as_a_backup_failure(self):
        """A backup that cannot be taken has to stop the run, loudly.

        The whole promise of the tool is that the run is reversible, so a
        silently incomplete backup is worse than no run at all.
        """
        missing = self.claude_root / "projects" / "-tmp-MyProject" / "gone.jsonl"
        with self.assertRaises(BackupError):
            back_up_claude_files(self.claude_root, [missing], self.backup_dir)


class TestBackUpRegistry(BackupTestCase):
    """Claude Code's per-project registry is saved whole, bytes and mode alike.

    The file stands in for ``~/.claude.json`` and lives inside a temporary
    directory; the real one is never read or written.
    """

    REGISTRY_TEXT = (
        '{\n  "projects": {\n    "/tmp/MyProject": {"allowedTools": ["Bash"]}\n  }\n}'
    )

    def setUp(self):
        super().setUp()
        self.registry = self.tmp / ".claude.json"
        self.registry.write_text(self.REGISTRY_TEXT, encoding="utf-8")
        self.backup_dir = new_backup_dir(
            self.backup_root, datetime(2026, 8, 29, 14, 30, 5)
        )

    def test_copies_the_file_under_a_fixed_name(self):
        """The whole file is copied, not only the keys about to move.

        A key rename is applied to the parsed document and the document is
        written out again, so the only reversal that can be trusted is putting
        the original bytes back.
        """
        target = back_up_registry(self.registry, self.backup_dir)

        self.assertEqual(target, self.backup_dir / REGISTRY_BACKUP_NAME)
        self.assertEqual(target.read_bytes(), self.registry.read_bytes())

    def test_permissions_are_preserved(self):
        """This file holds tool permissions and MCP servers for every project.

        A restore that widened who can read it would be a quiet downgrade of
        the user's security, so the mode travels with the bytes.
        """
        self.registry.chmod(0o600)
        target = back_up_registry(self.registry, self.backup_dir)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_a_missing_file_is_reported_as_a_backup_failure(self):
        """A backup that cannot be taken stops the run rather than proceeding.

        Nothing calls this unless the plan found keys to rename, so the file
        was there a moment ago; if it has gone, the state is not the one the
        plan describes and rewriting it blind is not safe.
        """
        with self.assertRaises(BackupError):
            back_up_registry(self.tmp / "absent.json", self.backup_dir)


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

    # Fake process tables. Each entry is one running process described the two
    # ways the shell can ask about it: what `ps -Ao comm=` prints, which is the
    # executable path with the arguments stripped, and what `pgrep -f` matches
    # against, which is the whole command line. They differ for real processes,
    # and that difference is the point - a fixture of bare executable paths
    # cannot tell the two implementations apart.
    #
    # NOTHING_RUNNING has no JetBrains process at all. IDE_RUNNING has IntelliJ
    # IDEA itself and must block an undo. TOOLBOX_RUNNING has only JetBrains
    # Toolbox and the JetBrains background daemon and must not.
    IDEA_EXECUTABLE = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"
    JETBRAINSD = (
        "/Users/tester/Library/Application Support/JetBrains/Daemon/bundles/"
        "current/jetbrainsd.app/Contents/MacOS/jetbrainsd"
    )
    TOOLBOX = "/Applications/JetBrains Toolbox.app/Contents/MacOS/jetbrains-toolbox"

    NOTHING_RUNNING = [
        ("/usr/sbin/cfprefsd", "/usr/sbin/cfprefsd"),
        ("/usr/libexec/secinitd", "/usr/libexec/secinitd"),
    ]
    IDE_RUNNING = [
        ("/usr/sbin/cfprefsd", "/usr/sbin/cfprefsd"),
        # Opened from a terminal, which is how the Toolbox shim launches it:
        # the project path arrives as an argument, so the command line does not
        # end at the executable name even though comm does.
        (IDEA_EXECUTABLE, f"{IDEA_EXECUTABLE} /Users/tester/project"),
    ]
    TOOLBOX_RUNNING = [
        ("/usr/sbin/cfprefsd", "/usr/sbin/cfprefsd"),
        (TOOLBOX, TOOLBOX),
        # The daemon really does run with an argument.
        (JETBRAINSD, f"{JETBRAINSD} run"),
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._stub_counter = itertools.count()

    def tearDown(self):
        self._tmp.cleanup()

    CLAUDE_PRE_TEXT = '{"project": "/OldLocation/MyProject"}\n'
    CLAUDE_POST_TEXT = '{"project": "/NewLocation/MyProject"}\n'

    @staticmethod
    def _registry_text(project: Path) -> str:
        """One registry document, keyed by the project's absolute path."""
        return json.dumps(
            {"projects": {str(project): {"allowedTools": ["Bash"]}}}, indent=2
        )

    def _build_scenario(
        self,
        *,
        create_dest=True,
        create_source=False,
        jetbrains_root=None,
        claude=False,
        claude_old_taken=False,
        registry=False,
    ):
        home = self.tmp / "home"
        # Where the IDEs keep their settings. Defaults to the standard
        # location under the fake home; a test that overrides it is checking
        # that the script restores where the manifest says, not where the
        # default would have put it.
        jetbrains_root = jetbrains_root or (
            home / "Library" / "Application Support" / "JetBrains"
        )
        options_dir = jetbrains_root / self.PRODUCT / "options"
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

        # Claude Code's side of the same migration, in the state it is in once
        # the run has finished: the entry directory already carries its
        # post-migration name and the history file inside it already mentions
        # the new location, while the backup holds a copy of that file at the
        # path it had before the rename.
        claude_root = home / ".claude"
        claude_old_entry = claude_root / "projects" / encode_project_path(source)
        claude_new_entry = claude_root / "projects" / encode_project_path(dest)
        claude_history = claude_root / "history.jsonl"
        claude_renames: list[list[str]] = []
        claude_root_recorded = None
        if claude:
            claude_new_entry.mkdir(parents=True)
            (claude_new_entry / "session.jsonl").write_text(
                self.CLAUDE_POST_TEXT, encoding="utf-8"
            )
            claude_history.write_text(self.CLAUDE_POST_TEXT, encoding="utf-8")
            if claude_old_taken:
                claude_old_entry.mkdir(parents=True)
                (claude_old_entry / "someone-elses.jsonl").write_text(
                    "{}\n", encoding="utf-8"
                )
            saved_claude = backup_dir / "claude"
            saved_claude.mkdir()
            (saved_claude / "history.jsonl").write_text(
                self.CLAUDE_PRE_TEXT, encoding="utf-8"
            )
            claude_renames = [[str(claude_old_entry), str(claude_new_entry)]]
            claude_root_recorded = str(claude_root)

        # Claude Code's per-project registry, in the state the run left it: the
        # key already names the new location, while the backup holds the file
        # as it was before, keyed by the old one. Restoring is a whole-file
        # copy, so those are the only two states there are.
        claude_registry = home / ".claude.json"
        claude_registry_recorded = None
        claude_registry_renames: list[list[str]] = []
        if registry:
            claude_registry.parent.mkdir(parents=True, exist_ok=True)
            claude_registry.write_text(
                self._registry_text(dest), encoding="utf-8"
            )
            (backup_dir / "claude-registry.json").write_text(
                self._registry_text(source), encoding="utf-8"
            )
            claude_registry_recorded = str(claude_registry)
            claude_registry_renames = [[str(source), str(dest)]]

        manifest = Manifest(
            version=1,
            tool_version="test",
            created_at="2026-08-29T14:30:05",
            home=str(home),
            jetbrains_root=str(jetbrains_root),
            source=str(source),
            dest=str(dest),
            move_status="moved",
            backed_up_products=[self.PRODUCT],
            rewritten_files={},
            undone_at=None,
            claude_root=claude_root_recorded,
            claude_renames=claude_renames,
            claude_registry=claude_registry_recorded,
            claude_registry_renames=claude_registry_renames,
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
            "claude_root": claude_root,
            "claude_old_entry": claude_old_entry,
            "claude_new_entry": claude_new_entry,
            "claude_history": claude_history,
            "claude_registry": claude_registry,
            "registry_before": self._registry_text(source),
            "registry_after": self._registry_text(dest),
        }

    def _stub_process_tools(self, processes):
        """Put a fake ``ps`` and a fake ``pgrep`` on PATH over one fixture.

        The real process table is never consulted, so these tests say nothing
        about what happens to be running on the machine.

        Both tools are stubbed from the same list of processes, each faithful
        to what the real one reports: ``ps -Ao comm=`` prints executable paths
        with arguments stripped, ``pgrep -f`` matches whole command lines. That
        makes these tests independent of which tool the script happens to use,
        and it is what lets them catch the difference between the two - a stub
        that fed both tools the same bare executable paths would report that an
        IDE launched with a project argument was not running.

        Neither stub returns a fixed answer: both honour the pattern the script
        supplies, because the script's own pattern is what decides whether a
        rollback is blocked.
        """
        stub_dir = self.tmp / f"stub_bin_{next(self._stub_counter)}"
        stub_dir.mkdir()

        comm_table = stub_dir / "comm_table"
        comm_table.write_text(
            "".join(f"{comm}\n" for comm, _command in processes), encoding="utf-8"
        )
        command_table = stub_dir / "command_table"
        command_table.write_text(
            "".join(f"{command}\n" for _comm, command in processes), encoding="utf-8"
        )

        ps = stub_dir / "ps"
        ps.write_text(
            "#!/bin/sh\n"
            "# Stands in for `ps -Ao comm=`: one executable path per line, no\n"
            "# arguments. The flags are ignored; this fixture has only the one\n"
            "# output format.\n"
            f'exec cat "{comm_table}"\n',
            encoding="utf-8",
        )
        ps.chmod(ps.stat().st_mode | stat.S_IXUSR)

        pgrep = stub_dir / "pgrep"
        pgrep.write_text(
            "#!/bin/sh\n"
            "# Stands in for `pgrep -f PATTERN`: the pattern arrives as $2 and\n"
            "# is matched against whole command lines, arguments included.\n"
            f'exec grep -E "$2" "{command_table}" >/dev/null 2>&1\n',
            encoding="utf-8",
        )
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
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

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
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = read_manifest(scenario["backup_dir"])
        self.assertIsNotNone(manifest.undone_at)

    def test_refuses_to_undo_twice(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

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
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

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
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(scenario["source"].exists())
        self.assertFalse(scenario["dest"].exists())
        self.assertTrue(
            (scenario["backup_dir"] / "config" / self.PRODUCT / "options").is_dir()
        )

    def test_path_argument_works_from_a_different_working_directory(self):
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_process_tools(self.NOTHING_RUNNING)
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
        """A running IDE must stop the rollback, arguments or no arguments.

        The IDE in this fixture was launched with a project path, which is what
        the Toolbox shell shim does. A check written against whole command
        lines cannot anchor on the executable name, so it sees nothing, allows
        the rollback, and the IDE then writes its in-memory settings over the
        restored files when it quits - silently undoing the undo.
        """
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_process_tools(self.IDE_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertNotEqual(result.returncode, 0)
        # It says which process it is refusing over, so the user knows what to
        # quit rather than having to guess.
        self.assertIn(self.IDEA_EXECUTABLE, result.stderr)
        self.assertTrue(scenario["dest"].is_dir())
        self.assertFalse(scenario["source"].exists())
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.POST_MIGRATION_TEXT,
        )

    def test_restores_into_the_configured_jetbrains_root(self):
        """A non-default settings directory must be restored, not recreated.

        The tool's configuration file can point jetbrains_root somewhere other
        than "~/Library/Application Support/JetBrains", and the Python undo
        already honours it. A script that assumed the default would create an
        empty settings tree in the wrong place, restore into it, and report
        success while the settings the user actually uses stayed broken.
        """
        custom_root = self.tmp / "custom-jetbrains"
        scenario = self._build_scenario(create_dest=True, jetbrains_root=custom_root)
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.PRE_MIGRATION_TEXT,
        )
        # Nothing was invented at the default location.
        default_root = scenario["home"] / "Library" / "Application Support" / "JetBrains"
        self.assertFalse(default_root.exists())

    def test_manifest_without_a_jetbrains_root_falls_back_to_the_default(self):
        """An older backup, written before the field existed, still restores."""
        scenario = self._build_scenario(create_dest=True)
        manifest_path = scenario["backup_dir"] / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        del data["jetbrains_root"]
        manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.PRE_MIGRATION_TEXT,
        )

    def test_renames_the_claude_entry_back_and_restores_its_files(self):
        """The script has to reverse the Claude Code step, not only the IDE one.

        undo.sh is the route that exists for when the tool itself is broken, so
        anything the migration does has to be reversible with nothing but bash
        and python3.
        """
        scenario = self._build_scenario(create_dest=True, claude=True)
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(scenario["claude_old_entry"].is_dir())
        self.assertFalse(scenario["claude_new_entry"].exists())
        self.assertEqual(
            scenario["claude_history"].read_text(encoding="utf-8"),
            self.CLAUDE_PRE_TEXT,
        )
        self.assertIn("Renamed Claude Code entry", result.stdout)
        self.assertIn("Restored Claude Code files", result.stdout)

    def test_the_summary_mentions_the_claude_entries(self):
        scenario = self._build_scenario(create_dest=True, claude=True)
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude 1 project entries renamed back", result.stdout)

    def test_declines_the_claude_rename_when_the_old_name_is_taken(self):
        """Something else at the old name means the state is not the recorded one.

        ``os.rename`` would replace it silently, so the script skips the rename
        and says why.
        """
        scenario = self._build_scenario(
            create_dest=True, claude=True, claude_old_taken=True
        )
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(scenario["claude_new_entry"].is_dir())
        self.assertEqual(
            (scenario["claude_old_entry"] / "someone-elses.jsonl").read_text(
                encoding="utf-8"
            ),
            "{}\n",
        )
        self.assertIn("already exists; left it alone", result.stdout)

    def test_restores_the_claude_project_registry(self):
        """undo.sh has to reverse the registry rewrite as well as the renames.

        This is the route that exists for when the tool itself is broken, so
        every part of the migration has to be reversible with nothing but bash
        and python3 - including the file holding each project's tool
        permissions and MCP servers.
        """
        scenario = self._build_scenario(
            create_dest=True, claude=True, registry=True
        )
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            scenario["claude_registry"].read_text(encoding="utf-8"),
            scenario["registry_before"],
        )
        self.assertIn(
            f"Restored Claude Code project registry {scenario['claude_registry']}",
            result.stdout,
        )

    def test_the_summary_mentions_the_registry(self):
        scenario = self._build_scenario(
            create_dest=True, claude=True, registry=True
        )
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude project registry restored (1 keys)", result.stdout)

    def test_a_manifest_without_the_registry_fields_leaves_the_file_alone(self):
        """An older backup has neither key, and must not touch the registry.

        A manifest written before this feature records no registry path, so
        there is nothing to restore and nothing to say. The registry sitting in
        the home directory belongs to runs this backup knows nothing about, and
        overwriting it from a copy that was never taken is not possible - but
        neither is inventing a default path and reading from it.
        """
        scenario = self._build_scenario(
            create_dest=True, claude=True, registry=True
        )
        manifest_path = scenario["backup_dir"] / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("claude_registry", "claude_registry_renames"):
            del data[key]
        manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        # The post-migration registry is exactly as the run left it.
        self.assertEqual(
            scenario["claude_registry"].read_text(encoding="utf-8"),
            scenario["registry_after"],
        )
        self.assertNotIn("registry", result.stdout)
        # The rest of the rollback still happened.
        self.assertTrue(scenario["source"].is_dir())
        self.assertTrue(scenario["claude_old_entry"].is_dir())

    def test_a_manifest_without_the_claude_fields_still_undoes(self):
        """An older backup carries neither the keys nor a saved claude tree.

        It has to roll back exactly as it always did, in silence about Claude
        Code rather than with an error.
        """
        scenario = self._build_scenario(create_dest=True)
        manifest_path = scenario["backup_dir"] / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("claude_root", "claude_renames", "claude_rewritten_files"):
            del data[key]
        manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        stub = self._stub_process_tools(self.NOTHING_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(scenario["source"].is_dir())
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.PRE_MIGRATION_TEXT,
        )
        self.assertNotIn("Claude Code", result.stdout)
        self.assertFalse(scenario["claude_root"].exists())

    def test_jetbrains_toolbox_alone_does_not_block_the_undo(self):
        """Toolbox is not an IDE, and must not stand between a user and undo.

        JetBrains Toolbox and the JetBrains daemon start themselves at login
        and are running on a normal machine essentially all the time. Neither
        holds IDE settings in memory, so neither can overwrite what the undo
        restores. A pattern loose enough to match them would make undo.sh -
        the recovery route the tool advertises - refuse to do anything on the
        very machines it is meant to rescue.
        """
        scenario = self._build_scenario(create_dest=True)
        stub = self._stub_process_tools(self.TOOLBOX_RUNNING)

        result = self._run_script(scenario["script"], stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(scenario["source"].is_dir())
        self.assertFalse(scenario["dest"].exists())
        self.assertEqual(
            scenario["options_file"].read_text(encoding="utf-8"),
            self.PRE_MIGRATION_TEXT,
        )


class BatchUndoScriptTestCase(unittest.TestCase):
    """Runs the generated undo.sh against a manifest recording several moves.

    The standalone script is the recovery route for when the tool itself is
    broken, so a batch run has to be reversible with nothing but bash and
    python3 - and in the same order, with the same wording, as the Python
    route. Each test builds its own fake home, its own set of moved projects
    and its own backup directory, then executes undo.sh as a subprocess.

    The process-table stubs and the subprocess runner are borrowed from
    ``TestUndoScriptBehavior`` rather than copied: the fake ``ps`` and ``pgrep``
    are what keep these tests from consulting the real process table, and there
    should be exactly one of them.
    """

    PRODUCT = "IntelliJIdea2026.2"
    PRE_MIGRATION_TEXT = "PRE-MIGRATION"
    POST_MIGRATION_TEXT = "POST-MIGRATION"

    NOTHING_RUNNING = TestUndoScriptBehavior.NOTHING_RUNNING
    _stub_process_tools = TestUndoScriptBehavior._stub_process_tools
    _run_script = TestUndoScriptBehavior._run_script

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._stub_counter = itertools.count()
        self.home = self.tmp / "home"
        self.jetbrains = (
            self.home / "Library" / "Application Support" / "JetBrains"
        )
        options_dir = self.jetbrains / self.PRODUCT / "options"
        options_dir.mkdir(parents=True)
        self.options_file = options_dir / "settings.xml"
        self.options_file.write_text(self.POST_MIGRATION_TEXT, encoding="utf-8")

        self.backup_dir = new_backup_dir(
            self.tmp / "Idea-Migration-Backups", datetime(2026, 8, 29, 14, 30, 5)
        )
        config_options = self.backup_dir / "config" / self.PRODUCT / "options"
        config_options.mkdir(parents=True)
        (config_options / "settings.xml").write_text(
            self.PRE_MIGRATION_TEXT, encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def source_of(self, name: str) -> Path:
        return self.home / name

    def dest_of(self, name: str) -> Path:
        return self.home / "Projects" / name

    def record(self, name: str, status: str = "moved") -> dict:
        return {
            "source": str(self.source_of(name)),
            "dest": str(self.dest_of(name)),
            "status": status,
            "rewritten_files": {},
            "claude_renames": [],
            "claude_rewritten_files": {},
            "registry_renames": [],
        }

    def place_at_destination(self, name: str) -> Path:
        dest = self.dest_of(name)
        dest.mkdir(parents=True)
        (dest / "marker.txt").write_text(name, encoding="utf-8")
        # The source's parent survives a real move - only the project itself
        # was relocated - so it is there for the rollback to land in.
        self.source_of(name).parent.mkdir(parents=True, exist_ok=True)
        return dest

    def place_at_source(self, name: str) -> Path:
        source = self.source_of(name)
        source.mkdir(parents=True)
        (source / "marker.txt").write_text(name, encoding="utf-8")
        return source

    def write_batch_manifest(self, moves, created_dirs=()) -> Path:
        write_manifest(
            self.backup_dir,
            Manifest(
                version=1,
                tool_version="test",
                created_at="2026-08-29T14:30:05",
                home=str(self.home),
                jetbrains_root=str(self.jetbrains),
                source=moves[0]["source"],
                dest=moves[0]["dest"],
                move_status="moved",
                backed_up_products=[self.PRODUCT],
                rewritten_files={},
                undone_at=None,
                moves=list(moves),
                created_dirs=[str(path) for path in created_dirs],
            ),
        )
        return write_undo_script(self.backup_dir)

    def run_undo(self, script):
        stub = self._stub_process_tools(self.NOTHING_RUNNING)
        result = self._run_script(script, stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result


class TestUndoScriptWithSeveralMoves(BatchUndoScriptTestCase):
    NAMES = ("Alpha", "Beta", "Gamma")

    def setUp(self):
        super().setUp()
        for name in self.NAMES:
            self.place_at_destination(name)
        self.script = self.write_batch_manifest(
            [self.record(name) for name in self.NAMES]
        )

    def test_every_directory_is_moved_back(self):
        self.run_undo(self.script)
        for name in self.NAMES:
            self.assertTrue(self.source_of(name).is_dir(), name)
            self.assertFalse(self.dest_of(name).exists(), name)
            self.assertEqual(
                (self.source_of(name) / "marker.txt").read_text(encoding="utf-8"),
                name,
            )

    def test_the_summary_lists_every_move_in_the_order_they_are_undone(self):
        """The summary is a promise about what is about to happen.

        Listing the moves in a different order from the one the script then
        applies would be worse than listing none at all, so the summary is
        pinned to the reversal order.
        """
        result = self.run_undo(self.script)

        summary = [
            line for line in result.stdout.splitlines() if line.startswith("  move")
        ]
        self.assertEqual(
            summary,
            [
                f"  move   {self.dest_of(name)} -> {self.source_of(name)}"
                for name in reversed(self.NAMES)
            ],
        )

    def test_the_moves_happen_in_reverse_order(self):
        result = self.run_undo(self.script)

        moved = [
            line for line in result.stdout.splitlines() if line.startswith("Moved ")
        ]
        self.assertEqual(
            moved,
            [
                f"Moved {self.dest_of(name)} back to {self.source_of(name)}"
                for name in reversed(self.NAMES)
            ],
        )

    def test_the_settings_are_still_restored(self):
        self.run_undo(self.script)
        self.assertEqual(
            self.options_file.read_text(encoding="utf-8"), self.PRE_MIGRATION_TEXT
        )

    def test_a_nested_destination_survives_the_reversal(self):
        """Reversing backwards is what makes a nested pair recoverable.

        The batch planner refuses to plan two moves whose sources and
        destinations overlap like this - it never lets a source sit inside
        another move's destination or vice versa. This test pins the undo
        script's reverse-order reversal on a hand-written manifest regardless,
        because an undo must cope with any manifest it is handed,
        planner-approved or not. Here the inner project's destination sits
        outside, but its source is inside the outer project's source; moving
        the outer one home first would put a directory where the inner
        reversal is about to write.
        """
        inner_source = self.source_of("Alpha") / "Inner"
        inner_dest = self.home / "Elsewhere" / "Inner"
        inner_dest.mkdir(parents=True)
        (inner_dest / "marker.txt").write_text("Inner", encoding="utf-8")
        script = self.write_batch_manifest(
            [
                self.record("Alpha"),
                {
                    "source": str(inner_source),
                    "dest": str(inner_dest),
                    "status": "moved",
                    "rewritten_files": {},
                    "claude_renames": [],
                    "claude_rewritten_files": {},
                    "registry_renames": [],
                },
            ]
        )

        self.run_undo(script)

        self.assertTrue(inner_source.is_dir())
        self.assertFalse(inner_dest.exists())
        self.assertEqual(
            (inner_source / "marker.txt").read_text(encoding="utf-8"), "Inner"
        )

    def test_the_manifest_is_stamped_undone(self):
        self.run_undo(self.script)
        self.assertIsNotNone(read_manifest(self.backup_dir).undone_at)


class TestUndoScriptWithAPartiallyFailedBatch(BatchUndoScriptTestCase):
    """The script has to roll back a run that died half way, like the tool does."""

    def setUp(self):
        super().setUp()
        self.place_at_destination("Alpha")
        # The failing move's directory had already left its source; what broke
        # was the work that came after it.
        self.place_at_destination("Beta")
        self.place_at_source("Gamma")
        self.script = self.write_batch_manifest(
            [
                self.record("Alpha", status="moved"),
                self.record("Beta", status="failed"),
                self.record("Gamma", status="pending"),
            ]
        )

    def test_the_completed_and_failed_moves_both_come_back(self):
        self.run_undo(self.script)
        for name in ("Alpha", "Beta"):
            self.assertTrue(self.source_of(name).is_dir(), name)
            self.assertFalse(self.dest_of(name).exists(), name)

    def test_the_move_that_never_started_is_reported_and_left_alone(self):
        result = self.run_undo(self.script)

        self.assertIn(
            f"Move {self.source_of('Gamma')} -> {self.dest_of('Gamma')} "
            "was never started; nothing to reverse.",
            result.stdout,
        )
        self.assertNotIn("not found", result.stdout)
        self.assertEqual(
            (self.source_of("Gamma") / "marker.txt").read_text(encoding="utf-8"),
            "Gamma",
        )

    def test_a_pending_record_whose_directory_did_move_is_still_moved_back(self):
        """What is on disk decides; the recorded status only picks the wording.

        Ctrl-C between the move and the manifest write leaves a "pending"
        record for a directory that has already gone. Believing the record
        would strand it at its new location.
        """
        script = self.write_batch_manifest([self.record("Alpha", status="pending")])

        result = self.run_undo(script)

        self.assertTrue(self.source_of("Alpha").is_dir())
        self.assertFalse(self.dest_of("Alpha").exists())
        self.assertIn(
            f"Moved {self.dest_of('Alpha')} back to {self.source_of('Alpha')}",
            result.stdout,
        )


class TestUndoScriptRemovesCreatedDirectories(BatchUndoScriptTestCase):
    def setUp(self):
        super().setUp()
        self.outer = self.home / "Projects"
        self.inner = self.outer / "Nested"
        self.source = self.source_of("Alpha")
        self.dest = self.inner / "Alpha"
        self.dest.mkdir(parents=True)
        (self.dest / "marker.txt").write_text("Alpha", encoding="utf-8")
        self.moves = [
            {
                "source": str(self.source),
                "dest": str(self.dest),
                "status": "moved",
                "rewritten_files": {},
                "claude_renames": [],
                "claude_rewritten_files": {},
                "registry_renames": [],
            }
        ]

    def test_empty_created_directories_are_removed_innermost_first(self):
        script = self.write_batch_manifest(
            self.moves, created_dirs=[self.outer, self.inner]
        )

        result = self.run_undo(script)

        self.assertFalse(self.inner.exists())
        self.assertFalse(self.outer.exists())
        removed = [
            line for line in result.stdout.splitlines() if line.startswith("Removed ")
        ]
        self.assertEqual(
            removed, [f"Removed {self.inner}", f"Removed {self.outer}"]
        )

    def test_a_directory_someone_else_used_is_left_alone(self):
        script = self.write_batch_manifest(
            self.moves, created_dirs=[self.outer, self.inner]
        )
        keeper = self.inner / "somebody-elses-project"
        keeper.mkdir()

        result = self.run_undo(script)

        self.assertTrue(keeper.is_dir())
        self.assertTrue(self.inner.is_dir())
        self.assertNotIn("Removed ", result.stdout)

    def test_nothing_is_removed_when_the_run_created_nothing(self):
        script = self.write_batch_manifest(self.moves)

        result = self.run_undo(script)

        self.assertTrue(self.outer.is_dir())
        self.assertNotIn("Removed ", result.stdout)


class TestUndoScriptWithAManifestWithoutMoves(BatchUndoScriptTestCase):
    """A backup written before batch runs existed still reverses, word for word.

    Its manifest has neither a ``moves`` list nor a ``created_dirs`` list, and
    the script has to fall back to the top-level source and dest and print
    exactly what it always printed for them.
    """

    def setUp(self):
        super().setUp()
        self.place_at_destination("Alpha")
        self.script = self.write_batch_manifest([self.record("Alpha")])
        manifest_path = self.backup_dir / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("moves", "created_dirs"):
            del data[key]
        manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_the_move_is_reversed_and_reported_as_before(self):
        result = self.run_undo(self.script)

        self.assertTrue(self.source_of("Alpha").is_dir())
        self.assertFalse(self.dest_of("Alpha").exists())
        self.assertIn(
            f"  move   {self.dest_of('Alpha')} -> {self.source_of('Alpha')}",
            result.stdout,
        )
        self.assertIn(
            f"Moved {self.dest_of('Alpha')} back to {self.source_of('Alpha')}",
            result.stdout,
        )

    def test_the_old_wording_is_unchanged_when_the_source_is_occupied(self):
        self.place_at_source("Alpha")

        result = self.run_undo(self.script)

        self.assertIn(
            f"Source {self.source_of('Alpha')} already exists; "
            "leaving the directory alone.",
            result.stdout,
        )

    def test_the_old_wording_is_unchanged_when_the_destination_is_gone(self):
        shutil.rmtree(self.dest_of("Alpha"))

        result = self.run_undo(self.script)

        self.assertIn(
            f"Destination {self.dest_of('Alpha')} not found; "
            "leaving the directory alone.",
            result.stdout,
        )


# One project's recorded external-system state, in the shape IntelliJ writes it.
CACHE_STATE_TEMPLATE = """<project version="4">
  <component name="ExternalSystemProjectTracker"><![CDATA[{{
  "projectData": {{"MAVEN": {{"api": {{"settingsTracker": {{"settingsFiles": {{
    "{project}/pom.xml": 4266877537
  }}}}}}}}}}
}}]]></component>
</project>
"""


class TestUndoScriptRestoresIdeCaches(unittest.TestCase):
    """undo.sh has to reverse the IDE cache rename, not only the Python route.

    The migration half of each test is done with the tool's own functions and
    the reversal with nothing but bash and python3, because that is the split
    that matters: undo.sh reimplements the boundary-anchored prefix rewrite
    rather than importing it, and the two have to agree exactly. A disagreement
    shows up here as a byte that did not come back.
    """

    PRODUCT = "IntelliJIdea2026.2"

    NOTHING_RUNNING = TestUndoScriptBehavior.NOTHING_RUNNING
    _stub_process_tools = TestUndoScriptBehavior._stub_process_tools
    _run_script = TestUndoScriptBehavior._run_script

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._stub_counter = itertools.count()

        self.home = self.tmp / "home"
        self.source = self.home / "IdeaProjects" / "api"
        self.dest = self.home / "Projects" / "cell" / "api"
        self.dest.mkdir(parents=True)

        self.caches_root = self.home / "Library" / "Caches" / "JetBrains"
        self.projects = self.caches_root / self.PRODUCT / "projects"
        self.projects.mkdir(parents=True)
        self.cache = self._build_cache(self.source)
        # The near-miss: a sibling whose path starts with the source's. Its own
        # hash is different, so nothing should ever match it.
        self.sibling = self._build_cache(self.home / "IdeaProjects" / "api-legacy")

        self.before = self._snapshot()

    def tearDown(self):
        self._tmp.cleanup()

    def _build_cache(self, project: Path) -> Path:
        cache = self.projects / f"{project.name}.{cache_suffix(project)}"
        (cache / "external_build_system").mkdir(parents=True)
        (cache / "cache-state.xml").write_text(
            CACHE_STATE_TEMPLATE.format(project=project.as_posix()), encoding="utf-8"
        )
        (cache / "external_build_system" / "modules.xml").write_text(
            '<project version="4">\n'
            '  <component name="ProjectModuleManager" />\n'
            "</project>\n",
            encoding="utf-8",
        )
        (cache / "indexingStamp.bin").write_bytes(b"\x00\x01\xff" * 8)
        return cache

    def _snapshot(self) -> dict:
        """Every path under the caches root, with its bytes or its kind."""
        digest = {}
        for path in sorted(self.caches_root.rglob("*")):
            key = str(path.relative_to(self.caches_root))
            digest[key] = "<dir>" if path.is_dir() else path.read_bytes()
        return digest

    def _migrate(self) -> list[tuple[str, str]]:
        """Do the forward half with the tool, and record it in a manifest."""
        variants = prefix_variants(self.source, self.dest, self.home)
        plan = plan_cache_renames(
            self.caches_root, [self.PRODUCT], self.source, self.dest, variants
        )
        performed = rename_caches(plan.renames)
        rewrite_caches([Path(new) for _old, new in performed], variants)

        self.backup_dir = new_backup_dir(
            self.tmp / "Idea-Migration-Backups", datetime(2026, 9, 10, 12, 0, 0)
        )
        write_manifest(
            self.backup_dir,
            Manifest(
                version=1,
                tool_version="test",
                created_at="2026-09-10T12:00:00",
                home=str(self.home),
                source=str(self.source),
                dest=str(self.dest),
                move_status="moved",
                backed_up_products=[],
                rewritten_files={},
                undone_at=None,
                jetbrains_root=str(
                    self.home / "Library" / "Application Support" / "JetBrains"
                ),
                caches_root=str(self.caches_root),
                cache_renames=[list(pair) for pair in performed],
            ),
        )
        self.script = write_undo_script(self.backup_dir)
        return performed

    def _undo(self):
        stub = self._stub_process_tools(self.NOTHING_RUNNING)
        result = self._run_script(self.script, stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_the_migration_really_renamed_and_repaired_the_cache(self):
        performed = self._migrate()
        moved = self.projects / f"api.{cache_suffix(self.dest)}"
        self.assertEqual(performed, [(str(self.cache), str(moved))])
        self.assertFalse(self.cache.exists())
        self.assertIn(
            f"{self.dest.as_posix()}/pom.xml",
            (moved / "cache-state.xml").read_text(encoding="utf-8"),
        )

    def test_undo_puts_every_byte_back(self):
        self._migrate()
        self._undo()
        self.assertEqual(self._snapshot(), self.before)

    def test_undo_reports_the_directory_it_renamed_back(self):
        self._migrate()
        result = self._undo()
        self.assertIn("Renamed IDE cache", result.stdout)
        self.assertIn(str(self.cache), result.stdout)

    def test_undo_lists_the_cache_in_its_summary(self):
        self._migrate()
        result = self._undo()
        self.assertIn("IDE module caches renamed back", result.stdout)

    def test_the_sibling_cache_is_never_touched(self):
        self._migrate()
        self.assertTrue(self.sibling.is_dir())
        self._undo()
        self.assertIn(
            (self.home / "IdeaProjects" / "api-legacy").as_posix(),
            (self.sibling / "cache-state.xml").read_text(encoding="utf-8"),
        )

    def test_a_manifest_with_no_cache_renames_says_nothing_about_caches(self):
        self._migrate()
        manifest = read_manifest(self.backup_dir)
        write_manifest(self.backup_dir, replace(manifest, cache_renames=[]))
        result = self._undo()
        self.assertNotIn("IDE cache", result.stdout)
        self.assertNotIn("IDE module caches renamed back", result.stdout)


if __name__ == "__main__":
    unittest.main()
