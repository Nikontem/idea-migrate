import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from idea_migrate.manifest import (
    MANIFEST_NAME,
    Manifest,
    MoveRecord,
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
        jetbrains_root="/Users/tester/Library/Application Support/JetBrains",
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


def claude_manifest() -> Manifest:
    """A manifest from a run that also relocated Claude Code project data."""
    return replace(
        sample_manifest(),
        claude_root="/Users/tester/.claude",
        claude_renames=[
            [
                "/Users/tester/.claude/projects/-Users-tester-WebstormProjects",
                "/Users/tester/.claude/projects/-Users-tester-Projects-WebstormProjects",
            ]
        ],
        claude_rewritten_files={"/Users/tester/.claude/history.jsonl": 12},
        claude_registry="/Users/tester/.claude.json",
        claude_registry_renames=[
            [
                "/Users/tester/WebstormProjects",
                "/Users/tester/Projects/WebstormProjects",
            ]
        ],
    )


class TestClaudeFields(unittest.TestCase):
    def test_they_default_to_empty(self):
        """A manifest constructed without them is valid and says "nothing".

        Every caller that predates the feature builds a Manifest by keyword
        without these three, so they have to be optional at construction as
        well as on read.
        """
        manifest = sample_manifest()
        self.assertIsNone(manifest.claude_root)
        self.assertEqual(manifest.claude_renames, [])
        self.assertEqual(manifest.claude_rewritten_files, {})
        self.assertIsNone(manifest.claude_registry)
        self.assertEqual(manifest.claude_registry_renames, [])

    def test_write_then_read_returns_equal_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            original = claude_manifest()
            write_manifest(backup_dir, original)
            restored = read_manifest(backup_dir)
            self.assertEqual(restored, original)
            # JSON has no tuples, so the renames must survive as lists - undo
            # unpacks them pair by pair and compares them to nothing else.
            self.assertEqual(restored.claude_renames[0][0], original.claude_renames[0][0])
            self.assertEqual(
                restored.claude_registry_renames[0][0],
                original.claude_registry_renames[0][0],
            )


class TestReadingAnOlderManifest(unittest.TestCase):
    def test_missing_claude_fields_read_as_nothing_to_undo(self):
        """A backup taken before this feature must stay loadable and undoable.

        The three keys are simply absent from such a file. They have to come
        back as "no Claude root recorded" and two empty collections, which
        every undo route reads as "this run touched nothing under ~/.claude"
        and skips in silence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, claude_manifest())
            path = backup_dir / MANIFEST_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("claude_root", "claude_renames", "claude_rewritten_files"):
                del data[key]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            restored = read_manifest(backup_dir)
            self.assertIsNone(restored.claude_root)
            self.assertEqual(restored.claude_renames, [])
            self.assertEqual(restored.claude_rewritten_files, {})

    def test_missing_claude_registry_fields_read_as_nothing_to_undo(self):
        """A backup taken before the registry-rewrite feature must stay loadable.

        The two registry keys are simply absent from such a file. They have to
        come back as "no Claude registry recorded" and an empty rename list,
        which every undo route reads as "this run did not touch the registry"
        and skips in silence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, claude_manifest())
            path = backup_dir / MANIFEST_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("claude_registry", "claude_registry_renames"):
                del data[key]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            restored = read_manifest(backup_dir)
            self.assertIsNone(restored.claude_registry)
            self.assertEqual(restored.claude_registry_renames, [])

    def test_missing_jetbrains_root_reads_as_not_recorded(self):
        """A backup written before the field existed must stay undoable.

        It loads with jetbrains_root as None, meaning "this manifest does not
        say", rather than raising or being filled in with a guess. Each undo
        route then applies its own fallback: the command chooses the
        configured settings directory, the standalone script chooses the
        default location. A value invented here would be indistinguishable
        from a recorded one, and the command would prefer the guess over the
        configuration the user actually passed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())
            path = backup_dir / MANIFEST_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["jetbrains_root"]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            self.assertIsNone(read_manifest(backup_dir).jetbrains_root)


def move_record(name: str, status: str = "moved") -> dict:
    """One plain-dict move record, in the shape ``Manifest.moves`` documents."""
    return {
        "source": f"/Users/tester/{name}",
        "dest": f"/Users/tester/Projects/{name}",
        "status": status,
        "rewritten_files": {f"/x/{name}/recentProjects.xml": 1},
        "claude_renames": [
            [
                f"/Users/tester/.claude/projects/-Users-tester-{name}",
                f"/Users/tester/.claude/projects/-Users-tester-Projects-{name}",
            ]
        ],
        "claude_rewritten_files": {f"/Users/tester/.claude/{name}.jsonl": 2},
        "registry_renames": [
            [f"/Users/tester/{name}", f"/Users/tester/Projects/{name}"]
        ],
    }


def batch_manifest() -> Manifest:
    """A manifest from a run that moved three directories in one batch."""
    return replace(
        sample_manifest(),
        moves=[move_record("Alpha"), move_record("Beta"), move_record("Gamma")],
        created_dirs=["/Users/tester/Projects", "/Users/tester/Projects/Nested"],
    )


class TestBatchFields(unittest.TestCase):
    def test_they_default_to_empty(self):
        """A manifest constructed without them is valid and describes one move.

        Every caller that predates batch runs builds a Manifest by keyword
        without ``moves`` or ``created_dirs``, so both have to be optional at
        construction as well as on read.
        """
        manifest = sample_manifest()
        self.assertEqual(manifest.moves, [])
        self.assertEqual(manifest.created_dirs, [])

    def test_write_then_read_returns_equal_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            original = batch_manifest()
            write_manifest(backup_dir, original)
            restored = read_manifest(backup_dir)
            self.assertEqual(restored, original)
            # JSON has no tuples, and each move record is a plain dict rather
            # than a typed object, so both have to survive the round trip in
            # exactly the shapes move_records() and the undo routes expect.
            self.assertEqual(len(restored.moves), 3)
            for record in restored.moves:
                self.assertIsInstance(record, dict)
            self.assertEqual(restored.moves[0]["source"], "/Users/tester/Alpha")
            self.assertEqual(
                restored.created_dirs,
                ["/Users/tester/Projects", "/Users/tester/Projects/Nested"],
            )
            for entry in restored.created_dirs:
                self.assertIsInstance(entry, str)


class TestMoveRecords(unittest.TestCase):
    def test_no_moves_list_returns_one_record_from_top_level_fields(self):
        """A pre-batch manifest describes exactly one move, built from the
        top-level fields rather than a ``moves`` entry.

        ``claude_renames`` and ``claude_registry_renames`` map into the
        record's ``claude_renames`` and ``registry_renames``, and
        ``move_status`` becomes the record's ``status`` - those are the
        top-level names the pre-batch fields were always recorded under.
        """
        manifest = claude_manifest()
        records = manifest.move_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.source, manifest.source)
        self.assertEqual(record.dest, manifest.dest)
        self.assertEqual(record.status, manifest.move_status)
        self.assertEqual(record.rewritten_files, manifest.rewritten_files)
        self.assertEqual(record.claude_renames, manifest.claude_renames)
        self.assertEqual(
            record.claude_rewritten_files, manifest.claude_rewritten_files
        )
        self.assertEqual(record.registry_renames, manifest.claude_registry_renames)

    def test_batch_manifest_returns_records_in_order_with_their_own_fields(self):
        manifest = batch_manifest()
        records = manifest.move_records()
        self.assertEqual(len(records), 3)
        for record, raw in zip(records, manifest.moves):
            self.assertEqual(record.source, raw["source"])
            self.assertEqual(record.dest, raw["dest"])
            self.assertEqual(record.status, raw["status"])
            self.assertEqual(record.rewritten_files, raw["rewritten_files"])
            self.assertEqual(record.claude_renames, raw["claude_renames"])
            self.assertEqual(
                record.claude_rewritten_files, raw["claude_rewritten_files"]
            )
            self.assertEqual(record.registry_renames, raw["registry_renames"])
        self.assertEqual(
            [record.source for record in records],
            ["/Users/tester/Alpha", "/Users/tester/Beta", "/Users/tester/Gamma"],
        )

    def test_record_missing_optional_keys_reads_as_pending_and_empty(self):
        """A record can carry only source and dest.

        The batch planner writes the rest as the move actually happens, and
        every reader has to treat the missing keys as "not moved yet" rather
        than raising.
        """
        manifest = replace(
            sample_manifest(),
            moves=[
                {
                    "source": "/Users/tester/Alpha",
                    "dest": "/Users/tester/Projects/Alpha",
                }
            ],
        )
        record = manifest.move_records()[0]
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.rewritten_files, {})
        self.assertEqual(record.claude_renames, [])
        self.assertEqual(record.claude_rewritten_files, {})
        self.assertEqual(record.registry_renames, [])


class TestReadingAnOlderManifestWithoutBatchFields(unittest.TestCase):
    def test_missing_moves_and_created_dirs_read_as_empty(self):
        """A backup taken before batch runs existed must stay loadable.

        The two keys are simply absent from such a file. They have to come
        back as empty lists, which move_records() then reads as "this
        manifest describes one move, in its top-level fields".
        """
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())
            path = backup_dir / MANIFEST_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("moves", "created_dirs"):
                del data[key]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            restored = read_manifest(backup_dir)
            self.assertEqual(restored.moves, [])
            self.assertEqual(restored.created_dirs, [])


class TestMoveRecordAsDict(unittest.TestCase):
    def test_as_dict_round_trips_to_the_record_dict_shape(self):
        record = MoveRecord(
            source="/Users/tester/Alpha",
            dest="/Users/tester/Projects/Alpha",
            status="moved",
            rewritten_files={"/x/Alpha/recentProjects.xml": 1},
            claude_renames=[
                [
                    "/Users/tester/.claude/projects/-Users-tester-Alpha",
                    "/Users/tester/.claude/projects/-Users-tester-Projects-Alpha",
                ]
            ],
            claude_rewritten_files={"/Users/tester/.claude/Alpha.jsonl": 2},
            registry_renames=[
                ["/Users/tester/Alpha", "/Users/tester/Projects/Alpha"]
            ],
        )
        self.assertEqual(
            record.as_dict(),
            {
                "source": "/Users/tester/Alpha",
                "dest": "/Users/tester/Projects/Alpha",
                "status": "moved",
                "rewritten_files": {"/x/Alpha/recentProjects.xml": 1},
                "claude_renames": [
                    [
                        "/Users/tester/.claude/projects/-Users-tester-Alpha",
                        "/Users/tester/.claude/projects/-Users-tester-Projects-Alpha",
                    ]
                ],
                "claude_rewritten_files": {"/Users/tester/.claude/Alpha.jsonl": 2},
                "cache_renames": [],
                "cache_rewritten_files": {},
                "registry_renames": [
                    ["/Users/tester/Alpha", "/Users/tester/Projects/Alpha"]
                ],
            },
        )


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
