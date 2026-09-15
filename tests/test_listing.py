import logging
import tempfile
import unittest
from pathlib import Path

from idea_migrate.listing import find_backups, format_backups
from idea_migrate.manifest import MANIFEST_NAME, Manifest, write_manifest


def make_backup(root: Path, stamp: str, undone: str | None = None) -> Path:
    backup_dir = root / stamp
    (backup_dir / "config").mkdir(parents=True)
    (backup_dir / "config" / "x.xml").write_text("<a/>", encoding="utf-8")
    write_manifest(
        backup_dir,
        Manifest(
            version=1,
            tool_version="0.1.0",
            created_at=stamp,
            home="/Users/tester",
            jetbrains_root="/Users/tester/Library/Application Support/JetBrains",
            source="/Users/tester/WebstormProjects",
            dest="/Users/tester/Projects/WebstormProjects",
            move_status="moved",
            backed_up_products=["IntelliJIdea2026.2"],
            rewritten_files={"/x.xml": 2},
            undone_at=undone,
        ),
    )
    return backup_dir


class TestFindBackups(unittest.TestCase):
    def test_returns_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            make_backup(root, "2026-08-30_100000")
            found = [s.directory.name for s in find_backups(root)]
            self.assertEqual(found, ["2026-08-30_100000", "2026-08-29_100000"])

    def test_reports_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            self.assertGreater(find_backups(root)[0].size_bytes, 0)

    def test_missing_root_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(find_backups(Path(tmp) / "absent"), [])

    def test_directory_without_manifest_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            (root / "junk").mkdir()
            self.assertEqual(len(find_backups(root)), 1)

    def test_corrupt_manifest_is_skipped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            corrupt_dir = root / "2026-08-28_100000"
            corrupt_dir.mkdir()
            (corrupt_dir / MANIFEST_NAME).write_text(
                "{not valid json", encoding="utf-8"
            )
            with self.assertLogs("idea_migrate.listing", level="WARNING") as cm:
                found = find_backups(root)
            self.assertEqual([s.directory.name for s in found], ["2026-08-29_100000"])
            self.assertTrue(
                any("2026-08-28_100000" in message for message in cm.output)
            )


def make_batch_backup(root: Path, stamp: str) -> Path:
    """A backup whose manifest records three moves made in one batch run."""
    backup_dir = root / stamp
    (backup_dir / "config").mkdir(parents=True)
    (backup_dir / "config" / "x.xml").write_text("<a/>", encoding="utf-8")
    moves = [
        {
            "source": "/Users/tester/Alpha",
            "dest": "/Users/tester/Projects/Alpha",
            "status": "moved",
            "rewritten_files": {"/x/Alpha.xml": 1},
            "claude_renames": [],
            "claude_rewritten_files": {},
            "registry_renames": [],
        },
        {
            "source": "/Users/tester/Beta",
            "dest": "/Users/tester/Projects/Beta",
            "status": "moved",
            "rewritten_files": {"/x/Beta.xml": 1},
            "claude_renames": [],
            "claude_rewritten_files": {},
            "registry_renames": [],
        },
        {
            "source": "/Users/tester/Gamma",
            "dest": "/Users/tester/Projects/Gamma",
            "status": "moved",
            "rewritten_files": {"/x/Gamma.xml": 1},
            "claude_renames": [],
            "claude_rewritten_files": {},
            "registry_renames": [],
        },
    ]
    write_manifest(
        backup_dir,
        Manifest(
            version=1,
            tool_version="0.1.0",
            created_at=stamp,
            home="/Users/tester",
            jetbrains_root="/Users/tester/Library/Application Support/JetBrains",
            source="/Users/tester/Alpha",
            dest="/Users/tester/Projects/Alpha",
            move_status="moved",
            backed_up_products=["IntelliJIdea2026.2"],
            rewritten_files={"/x/Alpha.xml": 1, "/x/Beta.xml": 1, "/x/Gamma.xml": 1},
            undone_at=None,
            moves=moves,
        ),
    )
    return backup_dir


class TestFormatBackupsWithABatch(unittest.TestCase):
    def test_lists_every_move_in_order_as_moved_arrow_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_batch_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            alpha = text.index("/Users/tester/Alpha")
            alpha_arrow = text.index("/Users/tester/Projects/Alpha")
            beta = text.index("/Users/tester/Beta")
            beta_arrow = text.index("/Users/tester/Projects/Beta")
            gamma = text.index("/Users/tester/Gamma")
            gamma_arrow = text.index("/Users/tester/Projects/Gamma")
            self.assertTrue(alpha < alpha_arrow < beta < beta_arrow < gamma < gamma_arrow)
            self.assertEqual(text.count("moved:"), 3)
            self.assertEqual(text.count("->"), 3)

    def test_still_shows_files_repaired_and_undo_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = make_batch_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            self.assertIn("files repaired: 3 across 1 products", text)
            self.assertIn(f"idea-migrate undo {backup_dir}", text)
            self.assertIn(str(backup_dir / "undo.sh"), text)

    def test_a_manifest_without_moves_is_listed_as_before(self):
        """A pre-batch manifest still yields exactly one moved/-> pair.

        move_records() falls back to the top-level source/dest for such a
        manifest, so the listing must show only that one pair - not zero, and
        not the batch layout leaking in from elsewhere.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            self.assertEqual(text.count("moved:"), 1)
            self.assertEqual(text.count("->"), 1)
            self.assertIn("/Users/tester/WebstormProjects", text)
            self.assertIn("/Users/tester/Projects/WebstormProjects", text)


class TestFormatBackups(unittest.TestCase):
    def test_empty_listing_mentions_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = format_backups([], root)
            self.assertIn(str(root), text)
            self.assertIn("No backups", text)

    def test_listing_shows_paths_and_undo_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            self.assertIn("WebstormProjects", text)
            self.assertIn("undo", text)
            self.assertIn("undo:", text)

    def test_undone_backups_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000", undone="2026-08-30T09:00:00")
            text = format_backups(find_backups(root), root)
            self.assertIn("rolled back", text.lower())

    def test_undone_backups_do_not_offer_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000", undone="2026-08-30T09:00:00")
            text = format_backups(find_backups(root), root)
            self.assertNotIn("undo:", text)

    def test_active_backups_offer_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            self.assertIn("undo:", text)

    def test_active_backups_offer_both_recovery_routes(self):
        """The listing must name the subcommand as well as the script.

        `idea-migrate undo` needs the tool installed and working; the
        standalone script needs only bash and python3. A user whose problem is
        the tool itself needs the script, and a user who has forgotten where
        the script is needs the subcommand.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = make_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            self.assertIn(f"idea-migrate undo {backup_dir}", text)
            self.assertIn(str(backup_dir / "undo.sh"), text)


if __name__ == "__main__":
    unittest.main()
