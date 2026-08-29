import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from idea_migrate.manifest import (
    MANIFEST_NAME,
    Manifest,
    mark_undone,
    read_manifest,
    write_manifest,
)


def sample_manifest() -> Manifest:
    return Manifest(
        version=1,
        tool_version="0.1.0",
        created_at="2026-08-29T14:30:05",
        home="/Users/tester",
        source="/Users/tester/WebstormProjects",
        dest="/Users/tester/Projects/WebstormProjects",
        move_status="moved",
        backed_up_products=["IntelliJIdea2026.2", "PyCharm2026.2"],
        rewritten_files={"/x/options/recentProjects.xml": 3},
        undone_at=None,
    )


class TestManifestRoundTrip(unittest.TestCase):
    def test_write_then_read_returns_equal_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            original = sample_manifest()
            write_manifest(backup_dir, original)
            self.assertEqual(read_manifest(backup_dir), original)

    def test_written_file_is_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())
            data = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(data["source"], "/Users/tester/WebstormProjects")
            self.assertIsNone(data["undone_at"])

    def test_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                read_manifest(Path(tmp))


class TestWriteManifestIsAtomic(unittest.TestCase):
    def test_overwrite_leaves_complete_file_and_no_leftover_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())

            second = replace(
                sample_manifest(),
                dest="/Users/tester/Projects/OtherProject",
                move_status="copied",
            )
            write_manifest(backup_dir, second)

            data = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(data["dest"], "/Users/tester/Projects/OtherProject")
            self.assertEqual(data["move_status"], "copied")

            self.assertEqual(
                [entry.name for entry in backup_dir.iterdir()], [MANIFEST_NAME]
            )


class TestMarkUndone(unittest.TestCase):
    def test_sets_undone_timestamp_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())
            updated = mark_undone(backup_dir, "2026-08-30T09:00:00")
            self.assertEqual(updated.undone_at, "2026-08-30T09:00:00")
            self.assertEqual(read_manifest(backup_dir).undone_at, "2026-08-30T09:00:00")


if __name__ == "__main__":
    unittest.main()
