import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from idea_migrate.claude_projects import (
    ClaudePlan,
    EntryRename,
    encode_project_path,
    json_variants,
    plan_entry_renames,
    preview_rewrites,
    relocate,
    rename_entries,
    rewritable_files,
    rewrite_files,
    rewrite_jsonl_file,
)
from idea_migrate.errors import ClaudeDataError, JsonIntegrityError

# These paths only ever appear inside file *content*, never on disk, so the
# rewriting tests can use realistic absolute paths without going near a real
# home directory.
OLD = Path("/Users/tester/IdeaProjects")
NEW = Path("/Users/tester/Projects/IdeaProjects")
VARIANTS = json_variants(OLD, NEW)


class TestEncodeProjectPath(unittest.TestCase):
    def test_separators_dots_and_underscores_all_become_dashes(self):
        self.assertEqual(
            encode_project_path(Path("/Users/nikos/.config/local_llm")),
            "-Users-nikos--config-local-llm",
        )

    def test_spaces_become_dashes(self):
        self.assertEqual(
            encode_project_path(Path("/Users/nikos/My Projects")),
            "-Users-nikos-My-Projects",
        )

    def test_letter_case_is_preserved(self):
        self.assertEqual(
            encode_project_path(Path("/Users/Nikos/IdeaProjects")),
            "-Users-Nikos-IdeaProjects",
        )

    def test_encoding_preserves_length(self):
        # plan_entry_renames splices a new prefix onto an existing entry name by
        # slicing at a fixed offset, which is only sound while this holds.
        path = Path("/Users/nikos/a_b.c d-e")
        self.assertEqual(len(encode_project_path(path)), len(path.as_posix()))


class ClaudeTreeTestCase(unittest.TestCase):
    """Each test builds its own fake home; the real ~/.claude is never read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.claude_root = self.home / ".claude"
        self.projects_dir = self.claude_root / "projects"
        self.projects_dir.mkdir(parents=True)
        self.source = self.home / "IdeaProjects"
        self.dest = self.home / "Projects" / "IdeaProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def make_project(self, *parts: str) -> Path:
        directory = self.source.joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def make_entry(self, name: str) -> Path:
        entry = self.projects_dir / name
        entry.mkdir()
        return entry

    def entry_for(self, directory: Path) -> Path:
        return self.make_entry(encode_project_path(directory))

    def new_name(self, directory: Path) -> str:
        """The entry name the given project directory should end up with."""
        source_encoded = encode_project_path(self.source)
        tail = encode_project_path(directory)[len(source_encoded) :]
        return encode_project_path(self.dest) + tail


class TestPlanEntryRenames(ClaudeTreeTestCase):
    def test_missing_projects_directory_gives_an_empty_plan(self):
        empty_root = self.home / "no-claude-here"
        plan = plan_entry_renames(empty_root, self.source, self.dest)
        self.assertEqual(plan, ClaudePlan(renames=[], unmatched=[]))

    def test_matches_the_source_entry_and_entries_below_it(self):
        self.make_project()
        child = self.make_project("api")
        grandchild = self.make_project("api", "deep")
        self.entry_for(self.source)
        self.entry_for(child)
        self.entry_for(grandchild)

        plan = plan_entry_renames(self.claude_root, self.source, self.dest)

        self.assertEqual(plan.unmatched, [])
        self.assertEqual(
            [(rename.old.name, rename.new.name) for rename in plan.renames],
            sorted(
                (encode_project_path(d), self.new_name(d))
                for d in (self.source, child, grandchild)
            ),
        )
        # Every rename records the real directory it stands for, at its old
        # location, so the caller can explain itself in the user's terms.
        self.assertEqual(
            {rename.project for rename in plan.renames},
            {self.source, child, grandchild},
        )

    def test_renames_are_sorted_by_the_old_entry_name(self):
        self.make_project()
        for name in ("zeta", "alpha", "mid"):
            self.entry_for(self.make_project(name))
        self.entry_for(self.source)

        plan = plan_entry_renames(self.claude_root, self.source, self.dest)
        names = [rename.old.name for rename in plan.renames]
        self.assertEqual(names, sorted(names))

    def test_a_longer_sibling_name_is_not_treated_as_a_candidate(self):
        """An entry for /x/foobar must not be dragged along by a move of /x/foo.

        Its name starts with the source's encoding but does not continue with a
        dash, so it belongs to a different directory entirely. It is neither
        renamed nor reported as unmatched - it has nothing to do with this move.
        """
        self.make_project()
        sibling = self.home / "IdeaProjectsArchive"
        sibling.mkdir()
        self.entry_for(self.source)
        sibling_entry = self.entry_for(sibling)

        plan = plan_entry_renames(self.claude_root, self.source, self.dest)

        self.assertEqual(
            [r.old.name for r in plan.renames],
            [encode_project_path(self.source)],
        )
        self.assertEqual(plan.unmatched, [])
        self.assertTrue(sibling_entry.is_dir())

    def test_an_entry_for_a_dashed_sibling_is_left_unmatched(self):
        """/x/foo-bar and /x/foo/bar encode to the same entry name.

        The encoding is lossy, so the name alone cannot say which directory it
        came from. With no /x/foo/bar on disk the entry is reported as unmatched
        and left alone rather than renamed on a guess.
        """
        self.make_project()
        dashed_sibling = self.home / "IdeaProjects-old"
        dashed_sibling.mkdir()
        self.entry_for(self.source)
        ambiguous = self.entry_for(dashed_sibling)

        plan = plan_entry_renames(self.claude_root, self.source, self.dest)

        self.assertEqual(
            [r.old.name for r in plan.renames],
            [encode_project_path(self.source)],
        )
        self.assertEqual(plan.unmatched, [ambiguous])

    def test_a_stale_entry_for_a_deleted_subdirectory_is_left_unmatched(self):
        self.make_project()
        self.entry_for(self.source)
        stale = self.make_entry(encode_project_path(self.source / "gone"))

        plan = plan_entry_renames(self.claude_root, self.source, self.dest)

        self.assertEqual(plan.unmatched, [stale])

    def test_entry_spelled_in_a_different_case_still_matches(self):
        """macOS is case-insensitive, so the entry may be spelled differently.

        The directory on disk is "Mixed" but Claude Code was run in a path typed
        entirely in lower case, so that is the spelling of the entry. It still
        has to be found, and the new name keeps the entry's own spelling of the
        tail rather than substituting the directory's.
        """
        self.make_project()
        mixed = self.make_project("MixedCase")
        self.entry_for(self.source)
        lowercase_entry = self.make_entry(encode_project_path(mixed).lower())

        plan = plan_entry_renames(self.claude_root, self.source, self.dest)

        matched = [r for r in plan.renames if r.old == lowercase_entry]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].project, mixed)
        self.assertTrue(matched[0].new.name.endswith("-mixedcase"))
        self.assertTrue(
            matched[0].new.name.startswith(encode_project_path(self.dest))
        )
        self.assertEqual(plan.unmatched, [])

    def test_walk_does_not_descend_where_no_entry_lies_below(self):
        """A node_modules tree must never be walked for nothing.

        Descending into every subdirectory of a large project would cost tens of
        thousands of directory listings on a project that has one entry.
        """
        self.make_project()
        keep = self.make_project("keep")
        heavy = self.make_project("node_modules", "pkg", "lib")
        self.entry_for(self.source)
        self.entry_for(keep)

        listed: list[str] = []
        real_scandir = os.scandir

        def spy(path):
            listed.append(str(path))
            return real_scandir(path)

        with mock.patch.object(os, "scandir", side_effect=spy):
            plan = plan_entry_renames(self.claude_root, self.source, self.dest)

        self.assertEqual(len(plan.renames), 2)
        self.assertIn(str(self.source), listed)
        self.assertNotIn(str(heavy.parent.parent), listed)
        self.assertNotIn(str(heavy.parent), listed)
        # The matched leaf is not listed either: nothing is left to find below it.
        self.assertNotIn(str(keep), listed)

    def test_an_existing_target_name_is_refused(self):
        self.make_project()
        self.entry_for(self.source)
        occupied = self.make_entry(encode_project_path(self.dest))

        with self.assertRaises(ClaudeDataError) as caught:
            plan_entry_renames(self.claude_root, self.source, self.dest)

        message = str(caught.exception)
        self.assertIn(encode_project_path(self.source), message)
        self.assertIn(occupied.name, message)


class TestJsonVariants(unittest.TestCase):
    def test_only_the_absolute_form_is_searched_for(self):
        # Claude Code's JSON holds plain absolute paths - no $USER_HOME$
        # placeholder and no file:// URLs - so one pair covers everything.
        self.assertEqual(
            json_variants(OLD, NEW),
            [("/Users/tester/IdeaProjects", "/Users/tester/Projects/IdeaProjects")],
        )


class TestRewritableFiles(ClaudeTreeTestCase):
    def test_history_comes_first_then_nested_transcripts_only(self):
        history = self.claude_root / "history.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        entry = self.entry_for(self.source)
        transcript = entry / "session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        subagents = entry / "session" / "subagents"
        subagents.mkdir(parents=True)
        nested = subagents / "agent.jsonl"
        nested.write_text("{}\n", encoding="utf-8")
        (entry / "custom-title.json").write_text("{}", encoding="utf-8")
        (entry / "memory").mkdir()
        (entry / "memory" / "notes.md").write_text("x", encoding="utf-8")

        rename = EntryRename(
            old=entry, new=self.projects_dir / "new", project=self.source
        )
        files = rewritable_files(self.claude_root, [rename])

        self.assertEqual(files[0], history)
        self.assertEqual(set(files[1:]), {transcript, nested})
        self.assertEqual(files[1:], sorted(files[1:]))

    def test_missing_history_file_is_simply_absent(self):
        entry = self.entry_for(self.source)
        rename = EntryRename(
            old=entry, new=self.projects_dir / "new", project=self.source
        )
        self.assertEqual(rewritable_files(self.claude_root, [rename]), [])

    def test_the_same_file_is_never_listed_twice(self):
        entry = self.entry_for(self.source)
        transcript = entry / "session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        rename = EntryRename(
            old=entry, new=self.projects_dir / "new", project=self.source
        )
        self.assertEqual(
            rewritable_files(self.claude_root, [rename, rename]), [transcript]
        )


class JsonlFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_jsonl(self, name: str, records: list[dict]) -> Path:
        path = self.tmp / name
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        return path


class TestRewriteJsonlFile(JsonlFileTestCase):
    def test_rewrites_the_cwd_value(self):
        path = self.write_jsonl(
            "session.jsonl",
            [{"cwd": "/Users/tester/IdeaProjects/foo"}, {"cwd": "/Users/tester/Other"}],
        )
        self.assertEqual(rewrite_jsonl_file(path, VARIANTS), 1)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[0]["cwd"], "/Users/tester/Projects/IdeaProjects/foo")
        self.assertEqual(records[1]["cwd"], "/Users/tester/Other")

    def test_rewrites_a_path_inside_a_nested_escaped_string(self):
        """Tool inputs arrive as JSON encoded inside a JSON string value.

        The path is then surrounded by escaped quotes, so the character right
        after it is a backslash rather than a quote. The JSON boundary rule has
        to accept that or half the references in a transcript are missed.
        """
        inner = json.dumps({"path": "/Users/tester/IdeaProjects/foo"})
        path = self.write_jsonl("session.jsonl", [{"input": inner}])
        raw = path.read_text(encoding="utf-8")
        self.assertIn('\\"/Users/tester/IdeaProjects/foo\\"', raw)

        self.assertEqual(rewrite_jsonl_file(path, VARIANTS), 1)

        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(record["input"])["path"],
            "/Users/tester/Projects/IdeaProjects/foo",
        )

    def test_a_longer_sibling_directory_is_left_alone(self):
        path = self.write_jsonl(
            "session.jsonl", [{"cwd": "/Users/tester/IdeaProjectsArchive/beta"}]
        )
        before = path.read_bytes()
        self.assertEqual(rewrite_jsonl_file(path, VARIANTS), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_a_sibling_whose_extra_word_follows_a_space_is_left_alone(self):
        # A space is legal in a macOS directory name, so it cannot end a path
        # component: "IdeaProjects Archive" is a different directory.
        path = self.write_jsonl(
            "session.jsonl", [{"cwd": "/Users/tester/IdeaProjects Archive/beta"}]
        )
        before = path.read_bytes()
        self.assertEqual(rewrite_jsonl_file(path, VARIANTS), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_crlf_endings_and_file_mode_are_preserved(self):
        path = self.tmp / "crlf.jsonl"
        path.write_bytes(
            b'{"cwd": "/Users/tester/IdeaProjects/a"}\r\n'
            b'{"cwd": "/Users/tester/IdeaProjects/b"}\r\n'
        )
        os.chmod(path, 0o600)

        self.assertEqual(rewrite_jsonl_file(path, VARIANTS), 2)

        data = path.read_bytes()
        self.assertEqual(data.count(b"\r\n"), 2)
        self.assertNotIn(b"\r\r", data)
        self.assertIn(b'"/Users/tester/Projects/IdeaProjects/a"', data)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_dry_run_reports_but_does_not_write(self):
        path = self.write_jsonl(
            "session.jsonl", [{"cwd": "/Users/tester/IdeaProjects/foo"}]
        )
        before = path.read_bytes()
        self.assertEqual(rewrite_jsonl_file(path, VARIANTS, dry_run=True), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_file_with_no_matches_is_untouched(self):
        path = self.write_jsonl("session.jsonl", [{"cwd": "/Users/tester/Downloads"}])
        before = path.stat().st_mtime_ns
        self.assertEqual(rewrite_jsonl_file(path, VARIANTS), 0)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_already_broken_file_is_skipped_with_a_warning(self):
        path = self.tmp / "broken.jsonl"
        path.write_bytes(
            b'{"cwd": "/Users/tester/IdeaProjects/a"}\n'
            b'{"cwd": "/Users/tester/IdeaProjects/b"\n'
        )
        before = path.read_bytes()

        with self.assertLogs("idea_migrate.claude_projects", level="WARNING") as logs:
            count = rewrite_jsonl_file(path, VARIANTS)

        self.assertEqual(count, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("already not valid JSON", logs.output[0])

    def test_a_rewrite_that_would_break_json_raises_and_writes_nothing(self):
        path = self.write_jsonl(
            "session.jsonl", [{"cwd": "/Users/tester/IdeaProjects/foo"}]
        )
        before = path.read_bytes()
        # A double quote in the destination would close the JSON string early.
        breaking = [("/Users/tester/IdeaProjects", '/Users/tester/Bad"Path')]

        with self.assertRaises(JsonIntegrityError):
            rewrite_jsonl_file(path, breaking)

        self.assertEqual(path.read_bytes(), before)

    def test_unreadable_file_is_skipped_with_a_warning(self):
        path = self.tmp / "missing" / "gone.jsonl"
        with self.assertLogs("idea_migrate.claude_projects", level="WARNING") as logs:
            count = rewrite_jsonl_file(path, VARIANTS)
        self.assertEqual(count, 0)
        self.assertIn("could not be read", logs.output[0])

    def test_non_utf8_file_is_skipped_with_a_warning(self):
        path = self.tmp / "latin1.jsonl"
        path.write_bytes(b'{"cwd": "\xff\xfe/Users/tester/IdeaProjects"}\n')
        with self.assertLogs("idea_migrate.claude_projects", level="WARNING") as logs:
            count = rewrite_jsonl_file(path, VARIANTS)
        self.assertEqual(count, 0)
        self.assertIn("not valid UTF-8", logs.output[0])


class TestRewriteFileSets(JsonlFileTestCase):
    def setUp(self):
        super().setUp()
        self.changed = self.write_jsonl(
            "changed.jsonl",
            [
                {"cwd": "/Users/tester/IdeaProjects/a"},
                {"cwd": "/Users/tester/IdeaProjects/b"},
            ],
        )
        self.untouched = self.write_jsonl(
            "untouched.jsonl", [{"cwd": "/Users/tester/Downloads"}]
        )

    def test_preview_lists_only_the_files_that_would_change(self):
        before = self.changed.read_bytes()
        result = preview_rewrites([self.changed, self.untouched], VARIANTS)
        self.assertEqual(result, {str(self.changed): 2})
        self.assertEqual(self.changed.read_bytes(), before)

    def test_rewrite_files_lists_only_the_files_that_changed(self):
        result = rewrite_files([self.changed, self.untouched], VARIANTS)
        self.assertEqual(result, {str(self.changed): 2})
        self.assertIn(
            "/Users/tester/Projects/IdeaProjects/a",
            self.changed.read_text(encoding="utf-8"),
        )
        # A second pass finds nothing left to do.
        self.assertEqual(rewrite_files([self.changed], VARIANTS), {})


class TestRenameEntries(ClaudeTreeTestCase):
    def test_performs_the_renames_and_reports_them(self):
        first = self.make_entry("-a-one")
        second = self.make_entry("-a-two")
        renames = [
            EntryRename(
                old=first, new=self.projects_dir / "-b-one", project=self.source
            ),
            EntryRename(
                old=second, new=self.projects_dir / "-b-two", project=self.source
            ),
        ]

        performed = rename_entries(renames)

        self.assertEqual(
            performed,
            [
                (str(first), str(self.projects_dir / "-b-one")),
                (str(second), str(self.projects_dir / "-b-two")),
            ],
        )
        self.assertFalse(first.exists())
        self.assertTrue((self.projects_dir / "-b-one").is_dir())
        self.assertTrue((self.projects_dir / "-b-two").is_dir())

    def test_a_source_that_vanished_is_skipped_with_a_warning(self):
        present = self.make_entry("-a-one")
        gone = self.projects_dir / "-a-gone"
        renames = [
            EntryRename(
                old=gone, new=self.projects_dir / "-b-gone", project=self.source
            ),
            EntryRename(
                old=present, new=self.projects_dir / "-b-one", project=self.source
            ),
        ]

        with self.assertLogs("idea_migrate.claude_projects", level="WARNING") as logs:
            performed = rename_entries(renames)

        # Only the rename that really happened is reported, so undo never tries
        # to reverse one that did not.
        self.assertEqual(
            performed, [(str(present), str(self.projects_dir / "-b-one"))]
        )
        self.assertIn("no longer exists", logs.output[0])
        self.assertFalse((self.projects_dir / "-b-gone").exists())


class TestRelocate(unittest.TestCase):
    def setUp(self):
        self.projects = Path("/home/.claude/projects")
        self.rename = EntryRename(
            old=self.projects / "-Users-tester-IdeaProjects",
            new=self.projects / "-Users-tester-Projects-IdeaProjects",
            project=OLD,
        )

    def test_a_file_under_a_renamed_entry_moves_with_it(self):
        before = self.rename.old / "session" / "subagents" / "agent.jsonl"
        self.assertEqual(
            relocate(before, [self.rename]),
            self.rename.new / "session" / "subagents" / "agent.jsonl",
        )

    def test_the_entry_directory_itself_maps_to_its_new_name(self):
        self.assertEqual(relocate(self.rename.old, [self.rename]), self.rename.new)

    def test_a_path_under_no_rename_is_returned_unchanged(self):
        history = Path("/home/.claude/history.jsonl")
        self.assertEqual(relocate(history, [self.rename]), history)

    def test_the_longest_matching_entry_wins(self):
        nested = EntryRename(
            old=self.rename.old / "inner",
            new=self.projects / "-nested",
            project=OLD / "inner",
        )
        target = nested.old / "x.jsonl"
        self.assertEqual(
            relocate(target, [self.rename, nested]), nested.new / "x.jsonl"
        )


if __name__ == "__main__":
    unittest.main()
