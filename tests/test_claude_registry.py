import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from idea_migrate import claude_registry
from idea_migrate.claude_registry import (
    REGISTRY_FILE_NAME,
    KeyRename,
    RegistryPlan,
    plan_registry_renames,
    rewrite_registry,
)
from idea_migrate.errors import ClaudeDataError, JsonIntegrityError

LOGGER = "idea_migrate.claude_registry"

# A realistic settings object: the values are never inspected or changed, so the
# tests only ever check that they come back exactly as they went in.
SETTINGS = {
    "allowedTools": ["Bash(git status:*)", "Read"],
    "mcpServers": {"pycharm": {"url": "http://127.0.0.1:63342/sse"}},
    "hasTrustDialogAccepted": True,
    "lastCost": 0.42,
}


class RegistryTestCase(unittest.TestCase):
    """Each test builds its own fake home; the real ~/.claude.json is never read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.registry = self.home / REGISTRY_FILE_NAME
        self.source = self.home / "IdeaProjects"
        self.dest = self.home / "Projects" / "IdeaProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def key(self, *parts: str) -> str:
        """The registry key for a directory inside the project being moved."""
        return self.source.joinpath(*parts).as_posix()

    def moved(self, *parts: str) -> str:
        """The key that same directory must end up under."""
        return self.dest.joinpath(*parts).as_posix()

    def write_registry(
        self, document: dict, indent: int = 2, trailing_newline: bool = False
    ) -> Path:
        text = json.dumps(document, indent=indent, ensure_ascii=False)
        if trailing_newline:
            text += "\n"
        self.registry.write_text(text, encoding="utf-8")
        return self.registry

    def write_projects(self, projects: dict, **kwargs) -> Path:
        return self.write_registry({"projects": projects}, **kwargs)

    def plan(self) -> RegistryPlan:
        return plan_registry_renames(self.registry, self.source, self.dest)

    def read_projects(self) -> dict:
        return json.loads(self.registry.read_text(encoding="utf-8"))["projects"]


class TestPlanRegistryRenames(RegistryTestCase):
    def test_the_key_for_the_moved_directory_itself_is_renamed(self):
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        self.assertEqual(plan.path, self.registry)
        self.assertEqual(plan.renames, [KeyRename(old=self.key(), new=self.moved())])

    def test_a_key_below_the_moved_directory_is_renamed(self):
        self.write_projects({self.key("api", "deep"): SETTINGS})
        self.assertEqual(
            self.plan().renames,
            [KeyRename(old=self.key("api", "deep"), new=self.moved("api", "deep"))],
        )

    def test_a_trailing_slash_survives_the_rename(self):
        """Claude Code sometimes records the cwd with a trailing separator.

        The key still names the directory being moved, so it has to move too, and
        it keeps the slash it was written with rather than being tidied up.
        """
        self.write_projects({self.key("api") + "/": SETTINGS})
        self.assertEqual(
            self.plan().renames,
            [KeyRename(old=self.key("api") + "/", new=self.moved("api") + "/")],
        )

    def test_a_key_spelled_in_a_different_case_still_matches(self):
        """macOS is case-insensitive, so one directory can leave several keys.

        Only the part that stands for the source is replaced; the key's own
        spelling of everything after it is kept, because that is what the user
        typed and there is no reason to second-guess it.
        """
        odd = self.source.as_posix().lower() + "/MixedCase"
        self.write_projects({odd: SETTINGS})
        self.assertEqual(
            self.plan().renames,
            [KeyRename(old=odd, new=self.moved("MixedCase"))],
        )

    def test_a_longer_sibling_key_is_left_alone(self):
        # /x/foobar starts with /x/foo but is a different directory entirely.
        sibling = str(self.home / "IdeaProjectsArchive") + "/beta"
        self.write_projects({sibling: SETTINGS})
        self.assertEqual(self.plan().renames, [])

    def test_a_sibling_whose_extra_word_follows_a_space_is_left_alone(self):
        # A space is legal in a macOS directory name, so it cannot end a path
        # component: "IdeaProjects Archive" is somewhere else.
        sibling = str(self.home / "IdeaProjects Archive") + "/beta"
        self.write_projects({sibling: SETTINGS})
        self.assertEqual(self.plan().renames, [])

    def test_keys_for_other_projects_are_left_alone(self):
        self.write_projects(
            {
                "/Users/tester/Downloads": SETTINGS,
                self.key("api"): SETTINGS,
                str(self.home / "Documents"): SETTINGS,
            }
        )
        self.assertEqual(
            self.plan().renames,
            [KeyRename(old=self.key("api"), new=self.moved("api"))],
        )

    def test_renames_come_back_in_the_order_the_file_lists_them(self):
        order = ["zeta", "alpha", "mid"]
        self.write_projects({self.key(name): SETTINGS for name in order})
        self.assertEqual(
            [rename.old for rename in self.plan().renames],
            [self.key(name) for name in order],
        )

    def test_a_missing_registry_gives_an_empty_plan_without_complaint(self):
        # A machine that has never run Claude Code is not a problem to report.
        with self.assertNoLogs(LOGGER, level="WARNING"):
            plan = self.plan()
        self.assertEqual(plan, RegistryPlan(path=self.registry, renames=[]))

    def test_a_registry_with_no_projects_key_gives_an_empty_plan(self):
        self.write_registry({"numStartups": 3})
        with self.assertNoLogs(LOGGER, level="WARNING"):
            plan = self.plan()
        self.assertEqual(plan.renames, [])

    def test_a_registry_with_no_matching_keys_gives_an_empty_plan(self):
        self.write_projects({"/Users/tester/Downloads": SETTINGS})
        with self.assertNoLogs(LOGGER, level="WARNING"):
            plan = self.plan()
        self.assertEqual(plan.renames, [])

    def test_malformed_json_is_skipped_with_a_warning(self):
        self.registry.write_text('{"projects": {', encoding="utf-8")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            plan = self.plan()
        self.assertEqual(plan.renames, [])
        self.assertIn("already not valid JSON", logs.output[0])

    def test_a_top_level_that_is_not_an_object_is_skipped_with_a_warning(self):
        self.registry.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            plan = self.plan()
        self.assertEqual(plan.renames, [])
        self.assertIn("not a JSON object", logs.output[0])

    def test_a_projects_value_that_is_not_an_object_is_skipped_with_a_warning(self):
        self.write_registry({"projects": [self.key()]})
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            plan = self.plan()
        self.assertEqual(plan.renames, [])
        self.assertIn("not a JSON object", logs.output[0])

    def test_a_registry_that_is_not_utf8_is_skipped_with_a_warning(self):
        self.registry.write_bytes(b'{"projects": {"\xff\xfe": {}}}')
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            plan = self.plan()
        self.assertEqual(plan.renames, [])
        self.assertIn("not valid UTF-8", logs.output[0])

    def test_a_key_that_is_already_taken_is_refused(self):
        """The destination is already listed, so renaming onto it would merge two
        projects' settings and silently discard one of them."""
        self.write_projects({self.key(): SETTINGS, self.moved(): SETTINGS})

        with self.assertRaises(ClaudeDataError) as caught:
            self.plan()

        message = str(caught.exception)
        self.assertIn(self.key(), message)
        self.assertIn(self.moved(), message)

    def test_two_keys_that_differ_only_in_case_are_refused(self):
        """Both keys name one directory, so both want the same new name.

        Nothing here can decide which project's settings should survive, so the
        run stops now - while the directory has not moved yet and there is
        nothing to undo.
        """
        lowercase = self.source.as_posix().lower() + "/api"
        self.write_projects({self.key("api"): SETTINGS, lowercase: {"lastCost": 1.0}})

        with self.assertRaises(ClaudeDataError) as caught:
            self.plan()

        message = str(caught.exception)
        self.assertIn(self.key("api"), message)
        self.assertIn(lowercase, message)

    def test_a_key_being_renamed_away_is_not_treated_as_an_obstacle(self):
        """Re-spelling a directory's own name is a legitimate move on macOS.

        Here IdeaProjects becomes ideaprojects, so every new key equals an old
        one as far as the case-insensitive collision check is concerned. The key
        that would be in the way is the very key being renamed, so it will be
        gone by the time the new name is used.
        """
        lowercased = self.home / "ideaprojects"
        self.write_projects({self.key("api"): SETTINGS})

        plan = plan_registry_renames(self.registry, self.source, lowercased)

        self.assertEqual(
            plan.renames,
            [
                KeyRename(
                    old=self.key("api"),
                    new=(lowercased / "api").as_posix(),
                )
            ],
        )


class TestRewriteRegistry(RegistryTestCase):
    def test_renames_the_keys_and_leaves_every_value_untouched(self):
        other = {"allowedTools": [], "mcpServers": {}}
        self.write_projects(
            {
                self.key(): SETTINGS,
                "/Users/tester/Downloads": other,
                self.key("api"): {"nested": {"deep": [1, 2, {"three": None}]}},
            }
        )
        plan = self.plan()

        self.assertEqual(rewrite_registry(self.registry, plan.renames), 2)

        projects = self.read_projects()
        self.assertEqual(projects[self.moved()], SETTINGS)
        self.assertEqual(projects["/Users/tester/Downloads"], other)
        self.assertEqual(
            projects[self.moved("api")], {"nested": {"deep": [1, 2, {"three": None}]}}
        )
        self.assertNotIn(self.key(), projects)
        self.assertNotIn(self.key("api"), projects)

    def test_key_order_is_preserved(self):
        order = [
            "/Users/tester/Downloads",
            self.key("zeta"),
            str(self.home / "Documents"),
            self.key("alpha"),
        ]
        self.write_projects({key: SETTINGS for key in order})
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        self.assertEqual(
            list(self.read_projects()),
            [
                "/Users/tester/Downloads",
                self.moved("zeta"),
                str(self.home / "Documents"),
                self.moved("alpha"),
            ],
        )

    def test_other_top_level_keys_keep_their_places(self):
        self.write_registry(
            {
                "numStartups": 7,
                "projects": {self.key(): SETTINGS},
                "oauthAccount": {"emailAddress": "tester@example.com"},
            }
        )
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        document = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(list(document), ["numStartups", "projects", "oauthAccount"])
        self.assertEqual(document["numStartups"], 7)
        self.assertEqual(
            document["oauthAccount"], {"emailAddress": "tester@example.com"}
        )

    def test_file_mode_is_preserved(self):
        # The registry holds an OAuth account and tool permissions, so a rewrite
        # must not widen its permissions.
        self.write_projects({self.key(): SETTINGS})
        os.chmod(self.registry, 0o600)
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)

    def test_a_file_with_no_trailing_newline_gets_none(self):
        # Claude Code writes the file without one; adding one would show up as a
        # spurious change in every diff of the user's dotfiles.
        self.write_projects({self.key(): SETTINGS}, trailing_newline=False)
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        self.assertFalse(self.registry.read_bytes().endswith(b"\n"))

    def test_a_file_with_a_trailing_newline_keeps_it(self):
        self.write_projects({self.key(): SETTINGS}, trailing_newline=True)
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        data = self.registry.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        self.assertFalse(data.endswith(b"\n\n"))

    def test_non_ascii_characters_stay_unescaped(self):
        cafe = str(self.home / "Café") + "/über"
        self.write_projects({self.key(): SETTINGS, cafe: {"note": "über"}})
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        data = self.registry.read_bytes()
        self.assertIn("Café".encode(), data)
        self.assertNotIn(b"\\u00e9", data)
        self.assertEqual(self.read_projects()[cafe], {"note": "über"})

    def test_a_four_space_indent_is_kept(self):
        self.write_projects({self.key(): SETTINGS}, indent=4)
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        lines = self.registry.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[1], '    "projects": {')

    def test_a_single_line_registry_comes_back_at_the_default_indent(self):
        # Nothing in the file says how it was indented, so the two spaces Claude
        # Code itself writes are used.
        self.registry.write_text(
            json.dumps({"projects": {self.key(): SETTINGS}}), encoding="utf-8"
        )
        plan = self.plan()

        rewrite_registry(self.registry, plan.renames)

        lines = self.registry.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[1], '  "projects": {')

    def test_dry_run_reports_but_does_not_write(self):
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        before = self.registry.read_bytes()

        self.assertEqual(
            rewrite_registry(self.registry, plan.renames, dry_run=True), 1
        )
        self.assertEqual(self.registry.read_bytes(), before)

    def test_a_second_pass_finds_nothing_left_to_do(self):
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        rewrite_registry(self.registry, plan.renames)
        before = self.registry.read_bytes()

        with self.assertLogs(LOGGER, level="WARNING"):
            count = rewrite_registry(self.registry, plan.renames)

        self.assertEqual(count, 0)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_a_key_that_vanished_after_planning_is_skipped_with_a_warning(self):
        """Claude Code may rewrite this file at any moment, including between the
        plan and the rewrite."""
        self.write_projects({self.key(): SETTINGS, self.key("api"): SETTINGS})
        plan = self.plan()
        self.write_projects({self.key("api"): SETTINGS})

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            count = rewrite_registry(self.registry, plan.renames)

        self.assertEqual(count, 1)
        self.assertIn("no longer listed", logs.output[0])
        self.assertEqual(list(self.read_projects()), [self.moved("api")])

    def test_a_key_whose_new_name_appeared_is_skipped_with_a_warning(self):
        # Never clobber: the settings now living under the new name belong to
        # somebody, and this run cannot tell whom.
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        self.write_projects({self.key(): SETTINGS, self.moved(): {"lastCost": 9.0}})

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            count = rewrite_registry(self.registry, plan.renames)

        self.assertEqual(count, 0)
        self.assertIn("already listed", logs.output[0])
        projects = self.read_projects()
        self.assertEqual(projects[self.key()], SETTINGS)
        self.assertEqual(projects[self.moved()], {"lastCost": 9.0})

    def test_a_rewrite_that_fails_verification_writes_nothing(self):
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        before = self.registry.read_bytes()

        # Stand in for any way the serialized text could stop matching the map
        # that was built: what gets written is checked, not what was intended.
        with mock.patch.object(
            claude_registry.json, "dumps", return_value='{"projects": {}}'
        ):
            with self.assertRaises(JsonIntegrityError):
                rewrite_registry(self.registry, plan.renames)

        self.assertEqual(self.registry.read_bytes(), before)

    def test_a_rewrite_producing_invalid_json_writes_nothing(self):
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        before = self.registry.read_bytes()

        with mock.patch.object(
            claude_registry.json, "dumps", return_value="{not json"
        ):
            with self.assertRaises(JsonIntegrityError):
                rewrite_registry(self.registry, plan.renames)

        self.assertEqual(self.registry.read_bytes(), before)

    def test_no_renames_means_no_work(self):
        self.write_projects({self.key(): SETTINGS})
        before = self.registry.read_bytes()
        self.assertEqual(rewrite_registry(self.registry, []), 0)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_a_missing_registry_is_not_an_error(self):
        renames = [KeyRename(old=self.key(), new=self.moved())]
        self.assertEqual(rewrite_registry(self.registry, renames), 0)
        self.assertFalse(self.registry.exists())

    def test_a_registry_that_became_malformed_is_skipped_with_a_warning(self):
        self.write_projects({self.key(): SETTINGS})
        plan = self.plan()
        self.registry.write_text('{"projects": {', encoding="utf-8")

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            count = rewrite_registry(self.registry, plan.renames)

        self.assertEqual(count, 0)
        self.assertIn("already not valid JSON", logs.output[0])


if __name__ == "__main__":
    unittest.main()
