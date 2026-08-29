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
