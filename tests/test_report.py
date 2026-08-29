import unittest
from pathlib import Path

from idea_migrate.report import format_plan, format_result

SOURCE = Path("/Users/tester/WebstormProjects")
DEST = Path("/Users/tester/Projects/WebstormProjects")
BACKUP = Path("/Users/tester/Idea-Migration-Backups/2026-08-29_143005")


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


class TestFormatResult(unittest.TestCase):
    def test_last_line_is_the_undo_command(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        last = text.rstrip().splitlines()[-1]
        self.assertIn("undo.sh", last)
        self.assertIn(str(BACKUP), last)

    def test_backup_path_appears_near_the_end(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn(str(BACKUP), text)

    def test_reports_replacement_totals(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3, "/b.xml": 2}, 0, [], BACKUP)
        self.assertIn("5", text)
        self.assertIn("2 files", text)

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


if __name__ == "__main__":
    unittest.main()
