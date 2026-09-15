import unittest
from pathlib import Path

from idea_migrate.claude_projects import EntryRename
from idea_migrate.claude_registry import KeyRename
from idea_migrate.report import (
    format_claude_dry_run_sections,
    format_claude_plan,
    format_plan,
    format_result,
)

SOURCE = Path("/Users/tester/WebstormProjects")
DEST = Path("/Users/tester/Projects/WebstormProjects")
BACKUP = Path("/Users/tester/Idea-Migration-Backups/2026-08-29_143005")

CLAUDE_ROOT = Path("/Users/tester/.claude")
ENTRIES = CLAUDE_ROOT / "projects"
RENAME = EntryRename(
    old=ENTRIES / "-Users-tester-WebstormProjects",
    new=ENTRIES / "-Users-tester-Projects-WebstormProjects",
    project=SOURCE,
)
UNMATCHED = [ENTRIES / "-Users-tester-WebstormProjects-old"]
REWRITES = {"/Users/tester/.claude/history.jsonl": 12}

REGISTRY = Path("/Users/tester/.claude.json")
KEY_RENAMES = [
    KeyRename(old=str(SOURCE), new=str(DEST)),
    KeyRename(old=f"{SOURCE}/alpha", new=f"{DEST}/alpha"),
]


class TestFormatPlan(unittest.TestCase):
    def test_shows_both_paths_and_counts(self):
        text = format_plan(SOURCE, DEST, 41, 15, BACKUP.parent)
        self.assertIn(str(SOURCE), text)
        self.assertIn(str(DEST), text)
        self.assertIn("41", text)
        self.assertIn("15", text)

    def test_mentions_where_the_backup_will_go(self):
        text = format_plan(SOURCE, DEST, 41, 15, BACKUP.parent)
        self.assertIn(str(BACKUP.parent), text)


class TestFormatClaudePlan(unittest.TestCase):
    def test_counts_entries_files_and_names_the_root(self):
        text = format_claude_plan([RENAME], REWRITES, [], CLAUDE_ROOT)
        self.assertIn(
            "  Claude Code project data: 1 entries to rename, 1 files to "
            f"rewrite (under {CLAUDE_ROOT}).",
            text,
        )

    def test_nothing_to_do_is_said_in_words(self):
        """Zero has to be explained, not printed as a row of zeroes.

        A project Claude Code has never been run in is the ordinary case, and
        "0 entries to rename, 0 files to rewrite" reads like a malfunction.
        """
        text = format_claude_plan([], {}, [], CLAUDE_ROOT)
        self.assertIn("No Claude Code project data refers to the old location", text)
        self.assertNotIn("0 entries to rename", text)

    def test_entries_left_alone_are_counted(self):
        """The one thing the tool declines to touch has to be visible.

        A near-miss entry is deliberately not renamed, and a user expecting
        their transcripts to follow the move needs to be told that before the
        run, not left to discover the gap afterwards.
        """
        text = format_claude_plan([RENAME], REWRITES, UNMATCHED, CLAUDE_ROOT)
        self.assertIn("1 similarly named entry will be left alone.", text)

    def test_nothing_is_said_about_left_alone_entries_when_there_are_none(self):
        text = format_claude_plan([RENAME], REWRITES, [], CLAUDE_ROOT)
        self.assertNotIn("left alone", text)

    def test_registry_keys_are_counted_and_the_file_is_named(self):
        """The registry is a separate file, so it gets a line of its own.

        Naming it matters: it is not under the Claude Code data directory the
        line above mentions, and a user checking a plan needs to know which
        file is about to be edited.
        """
        text = format_claude_plan(
            [RENAME],
            REWRITES,
            [],
            CLAUDE_ROOT,
            registry_renames=KEY_RENAMES,
            registry_path=REGISTRY,
        )
        self.assertIn(
            f"  Claude Code project registry: 2 keys to rename in {REGISTRY}.",
            text,
        )

    def test_a_single_registry_key_is_singular(self):
        text = format_claude_plan(
            [RENAME],
            REWRITES,
            [],
            CLAUDE_ROOT,
            registry_renames=KEY_RENAMES[:1],
            registry_path=REGISTRY,
        )
        self.assertIn(
            f"  Claude Code project registry: 1 key to rename in {REGISTRY}.",
            text,
        )

    def test_nothing_is_said_about_the_registry_when_no_key_moves(self):
        """A registry with nothing to rename adds no line at all.

        That holds whether or not there is other Claude Code work to report. A
        machine may have no ``~/.claude.json``, or one that never mentions the
        directory being moved, and either way there is nothing about it worth a
        line in the plan.
        """
        with_entries = format_claude_plan([RENAME], REWRITES, [], CLAUDE_ROOT)
        self.assertNotIn("registry", with_entries)

        with_nothing = format_claude_plan([], {}, [], CLAUDE_ROOT)
        self.assertNotIn("registry", with_nothing)
        # The existing sentence about the entries is untouched by the addition.
        self.assertIn(
            "No Claude Code project data refers to the old location", with_nothing
        )


class TestFormatClaudeDryRunSections(unittest.TestCase):
    def test_rename_pairs_are_shown_old_above_new(self):
        text = format_claude_dry_run_sections([RENAME], {}, [])
        self.assertIn("Claude Code project entries that would be renamed:", text)
        self.assertIn(f"  {RENAME.old}\n    ->   {RENAME.new}\n", text)

    def test_rewritable_files_are_listed_with_their_counts(self):
        text = format_claude_dry_run_sections([], REWRITES, [])
        self.assertIn("Claude Code files that would be rewritten:", text)
        self.assertIn("/Users/tester/.claude/history.jsonl  (12 references)", text)

    def test_a_single_reference_is_singular(self):
        text = format_claude_dry_run_sections([], {"/a.jsonl": 1}, [])
        self.assertIn("/a.jsonl  (1 reference)", text)

    def test_entries_left_alone_are_named_with_the_reason(self):
        text = format_claude_dry_run_sections([], {}, UNMATCHED)
        self.assertIn(
            "Claude Code entries left alone (similar name, but no matching "
            "directory under the source):",
            text,
        )
        self.assertIn(str(UNMATCHED[0]), text)

    def test_empty_sections_are_omitted_entirely(self):
        """Nothing to report must print nothing, not three empty headings."""
        self.assertEqual(format_claude_dry_run_sections([], {}, []), "")
        only_renames = format_claude_dry_run_sections([RENAME], {}, [])
        self.assertNotIn("would be rewritten", only_renames)
        self.assertNotIn("left alone", only_renames)

    def test_the_block_ends_with_a_blank_line(self):
        """It is printed between two other blocks, so it separates itself."""
        text = format_claude_dry_run_sections([RENAME], REWRITES, UNMATCHED)
        self.assertTrue(text.endswith("\n\n"), repr(text[-20:]))

    def test_registry_key_pairs_are_shown_old_above_new(self):
        """The keys are listed in full, not counted.

        A registry key is an absolute path and the rename is a prefix
        substitution, so seeing each pair is how a user confirms the tool
        matched the directories it meant to rather than a similarly named
        sibling.
        """
        text = format_claude_dry_run_sections(
            [], {}, [], registry_renames=KEY_RENAMES, registry_path=REGISTRY
        )
        self.assertIn(
            f"Claude Code project registry keys that would be renamed "
            f"(in {REGISTRY}):",
            text,
        )
        for rename in KEY_RENAMES:
            self.assertIn(f"  {rename.old}\n    ->   {rename.new}\n", text)

    def test_the_registry_section_is_omitted_when_no_key_moves(self):
        text = format_claude_dry_run_sections([RENAME], REWRITES, UNMATCHED)
        self.assertNotIn("registry", text)
        # And with nothing at all to report, still nothing at all.
        self.assertEqual(
            format_claude_dry_run_sections([], {}, [], registry_path=REGISTRY), ""
        )

    def test_the_block_still_ends_with_a_blank_line_with_only_a_registry(self):
        """The registry section separates itself like every other one."""
        text = format_claude_dry_run_sections(
            [], {}, [], registry_renames=KEY_RENAMES, registry_path=REGISTRY
        )
        self.assertTrue(text.endswith("\n\n"), repr(text[-20:]))


class TestFormatResult(unittest.TestCase):
    def test_last_line_is_the_undo_command(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        last = text.rstrip().splitlines()[-1]
        self.assertIn("undo.sh", last)
        self.assertIn(str(BACKUP), last)

    def test_both_recovery_routes_are_offered(self):
        """The report must name the subcommand as well as the script.

        The two routes fail differently: `idea-migrate undo` needs the tool
        itself to still work, while the standalone script needs only bash and
        python3. Naming only one leaves a user stuck whenever that one is the
        route that is broken.
        """
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn(f"idea-migrate undo {BACKUP}", text)
        self.assertIn(str(BACKUP / "undo.sh"), text)

    def test_recovery_commands_are_the_closing_lines(self):
        """Recovery must not be buried in the middle of the output."""
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        lines = [line.strip() for line in text.rstrip().splitlines()]
        self.assertIn(f"Backup saved to: {BACKUP}", lines)
        self.assertEqual(lines[-2], f"idea-migrate undo {BACKUP}")
        self.assertEqual(lines[-1], str(BACKUP / "undo.sh"))

    def test_backup_path_appears_near_the_end(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn(str(BACKUP), text)

    def test_reports_replacement_totals(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3, "/b.xml": 2}, 0, [], BACKUP)
        self.assertIn("5", text)
        self.assertIn("2 files", text)

    def test_nothing_to_repair_says_so_in_words(self):
        """Zero must be explained, not just printed.

        "Repaired 0 path references in 0 files" reads like something went
        wrong. The report has to say which it was: the search ran and there
        was nothing in any configuration file to change.
        """
        text = format_result(SOURCE, DEST, {}, 0, [], BACKUP)
        self.assertIn("No path references needed repairing", text)
        self.assertNotIn("Repaired 0 path references", text)

    def test_surviving_references_are_flagged(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 4, [], BACKUP)
        self.assertIn("4", text)
        self.assertIn("still", text.lower())

    def test_hardcoded_path_warnings_are_shown(self):
        text = format_result(
            SOURCE, DEST, {"/a.xml": 3}, 0, ["/x/.idea/runConfigurations/a.xml"], BACKUP
        )
        self.assertIn("runConfigurations", text)

    def test_mentions_reindexing(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn("index", text.lower())

    def test_claude_work_is_reported_when_there_was_any(self):
        text = format_result(
            SOURCE,
            DEST,
            {"/a.xml": 3},
            0,
            [],
            BACKUP,
            claude_renames=[RENAME],
            claude_rewritten={"/h.jsonl": 12, "/t.jsonl": 4},
        )
        self.assertIn(
            "  Renamed 1 Claude Code project entries and repaired 16 "
            "references in 2 of their files.",
            text,
        )

    def test_no_claude_work_is_said_in_words(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn("No Claude Code project data referred to the old location", text)

    def test_registry_keys_renamed_are_reported_with_the_file(self):
        text = format_result(
            SOURCE,
            DEST,
            {"/a.xml": 3},
            0,
            [],
            BACKUP,
            registry_renamed=2,
            registry_path=REGISTRY,
        )
        self.assertIn(
            f"  Renamed 2 keys in the Claude Code project registry ({REGISTRY}).",
            text,
        )

    def test_nothing_is_said_about_the_registry_when_no_key_was_renamed(self):
        """No line at all, rather than a "nothing to do" sentence.

        The two counts above it do explain their zero, because a project with
        no Claude Code history is worth confirming. A registry is different: a
        machine may simply not have one, and reporting on a file that does not
        exist raises a question instead of answering it.
        """
        text = format_result(
            SOURCE,
            DEST,
            {"/a.xml": 3},
            0,
            [],
            BACKUP,
            registry_renamed=0,
            registry_path=REGISTRY,
        )
        self.assertNotIn("registry", text)

    def test_the_claude_arguments_are_optional(self):
        """A caller that says nothing about Claude Code still gets a report.

        The two arguments are keyword-only with empty defaults so that the
        existing call sites, and any test written before the feature, keep
        working unchanged.
        """
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn("Migration complete", text)


class TestIdeCacheReporting(unittest.TestCase):
    """The stored module definitions get their own lines, in plan and result."""

    SOURCE = Path("/Users/tester/IdeaProjects/api")
    DEST = Path("/Users/tester/Projects/api")
    BACKUP = Path("/Users/tester/Idea-Migration-Backups/2026-09-10_120000")

    def plan(self, cache_count):
        return format_plan(
            self.SOURCE,
            self.DEST,
            3,
            2,
            Path("/Users/tester/Idea-Migration-Backups"),
            cache_count=cache_count,
        )

    def result(self, **kwargs):
        return format_result(
            self.SOURCE, self.DEST, {}, 0, [], self.BACKUP, **kwargs
        )

    def test_the_plan_says_nothing_when_there_is_no_stored_module_data(self):
        self.assertNotIn("stored module", self.plan(0))

    def test_the_plan_counts_the_stores_and_explains_the_undo(self):
        text = self.plan(2)
        self.assertIn("2 stored module stores will move with the project", text)
        self.assertIn("undo reverses them without a backup", text)

    def test_the_plan_inflects_a_single_store(self):
        self.assertIn("1 stored module store will move", self.plan(1))

    def test_the_result_is_silent_when_nothing_moved(self):
        self.assertNotIn("stored module", self.result())

    def test_the_result_tallies_what_moved_and_what_was_repaired(self):
        text = self.result(
            cache_renamed=[("/caches/api.aaaa", "/caches/api.bbbb")],
            cache_rewritten={"/caches/api.bbbb/cache-state.xml": 1},
        )
        self.assertIn(
            "Moved 1 stored module store and repaired 1 references in 1 of "
            "their files.",
            text,
        )

    def test_the_relink_warning_names_the_symptom_and_the_fix(self):
        text = self.result(relink_required=True)
        self.assertIn("empty", text)
        self.assertIn("Maven or Gradle tool window", text)
        self.assertIn("add it as a", text)

    def test_no_relink_warning_when_nothing_needs_relinking(self):
        self.assertNotIn("Maven or Gradle tool window", self.result())

    def test_the_closing_advice_no_longer_calls_every_cache_self_healing(self):
        # The claim it replaced was wrong: the module definitions are the one
        # cache that does not rebuild itself, which is the whole reason the
        # tool moves them.
        text = self.result()
        self.assertIn("the one part that", text)
        self.assertIn("is not", text)


if __name__ == "__main__":
    unittest.main()
