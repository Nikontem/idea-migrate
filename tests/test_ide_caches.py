import tempfile
import unittest
from pathlib import Path

from idea_migrate.errors import IdeCacheError
from idea_migrate.ide_caches import (
    CacheRename,
    cache_files,
    cache_suffix,
    java_string_hash,
    plan_cache_renames,
    rename_caches,
    rewrite_caches,
    uses_external_storage,
)
from idea_migrate.rewrite import prefix_variants

HOME = Path("/Users/tester")
SOURCE = HOME / "IdeaProjects" / "api"
DEST = HOME / "Projects" / "cell" / "api"
VARIANTS = prefix_variants(SOURCE, DEST, HOME)

# The one absolute path a cache directory records, in the shape IntelliJ writes
# it: a JSON blob inside a CDATA section keyed by the build file's path.
CACHE_STATE = """<project version="4">
  <component name="ExternalSystemProjectTracker"><![CDATA[{{
  "projectData": {{
    "MAVEN": {{
      "api": {{
        "settingsTracker": {{
          "settingsFiles": {{
            "{project}/pom.xml": 4266877537
          }}
        }}
      }}
    }}
  }}
}}]]></component>
</project>
"""

# Module definitions are written relative to $MODULE_DIR$, so a move leaves them
# alone. Kept in the fixtures to prove the rewrite does not touch them.
MODULE_XML = """<module external.linked.project.id="$MODULE_DIR$/pom.xml">
  <component name="NewModuleRootManager" />
</module>
"""

MISC_EXTERNAL = """<project version="4">
  <component name="ExternalStorageConfigurationManager" enabled="true" />
</project>
"""

MISC_LOCAL = """<project version="4">
  <component name="ExternalStorageConfigurationManager" enabled="false" />
</project>
"""


def build_cache(directory: Path, project: Path) -> Path:
    """Create a cache directory for ``project`` under ``directory``."""
    cache = directory / f"{project.name}.{cache_suffix(project)}"
    (cache / "external_build_system" / "modules").mkdir(parents=True)
    (cache / "cache-state.xml").write_text(
        CACHE_STATE.format(project=project.as_posix()), encoding="utf-8"
    )
    (cache / "external_build_system" / "modules" / "api.xml").write_text(
        MODULE_XML, encoding="utf-8"
    )
    # A binary neighbour, to prove only XML is read and rewritten.
    (cache / "indexingStamp.bin").write_bytes(b"\x00\x01\xff" * 8)
    return cache


class TestJavaStringHash(unittest.TestCase):
    # Golden values taken from real cache directories on a machine that had run
    # IntelliJ 2026.2. They are the whole reason the lookup works, so they are
    # pinned rather than recomputed.
    KNOWN = {
        "/Users/nikosntemkas/IdeaProjects/ai-qa-summarization-ts": "7eeb8bf3",
        "/Users/nikosntemkas/Projects/IdeaProjects/ai-qa-summarization-ts": "1f4b337a",
        "/Users/nikosntemkas/Projects/cell/components/abstraction-layer"
        "/ai-qa-summarization-ts": "a8dc461b",
        "/Users/nikosntemkas/IdeaProjects/cell-qa-summarization-ms": "31976754",
    }

    def test_matches_directory_names_written_by_the_ide(self):
        for path, expected in self.KNOWN.items():
            with self.subTest(path=path):
                self.assertEqual(cache_suffix(Path(path)), expected)

    def test_empty_string_hashes_to_zero(self):
        self.assertEqual(java_string_hash(""), 0)

    def test_hash_is_unsigned_32_bit(self):
        # Java's own hash overflows into negative numbers; the hex spelling the
        # directory name uses is of the unsigned value.
        value = java_string_hash("/Users/tester/some/deeply/nested/project/path")
        self.assertGreaterEqual(value, 0)
        self.assertLess(value, 2**32)

    def test_suffix_is_not_zero_padded(self):
        # Integer.toHexString drops leading zeros, so a small hash gives a short
        # directory name. Padding it would look for a directory that is not there.
        self.assertEqual(format(java_string_hash("\x01"), "x"), "1")


class TestUsesExternalStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "api"
        (self.project / ".idea").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_true_when_the_component_is_enabled(self):
        (self.project / ".idea" / "misc.xml").write_text(
            MISC_EXTERNAL, encoding="utf-8"
        )
        self.assertTrue(uses_external_storage(self.project))

    def test_false_when_the_component_is_disabled(self):
        (self.project / ".idea" / "misc.xml").write_text(
            MISC_LOCAL, encoding="utf-8"
        )
        self.assertFalse(uses_external_storage(self.project))

    def test_false_when_there_is_no_misc_file(self):
        self.assertFalse(uses_external_storage(self.project))

    def test_false_when_the_file_is_malformed(self):
        (self.project / ".idea" / "misc.xml").write_text(
            "<project", encoding="utf-8"
        )
        self.assertFalse(uses_external_storage(self.project))


class TestPlanCacheRenames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.caches = Path(self.tmp.name) / "Caches" / "JetBrains"
        self.projects = self.caches / "IntelliJIdea2026.2" / "projects"
        self.projects.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def plan(self, source=SOURCE, dest=DEST, products=("IntelliJIdea2026.2",)):
        return plan_cache_renames(
            self.caches, products, source, dest, prefix_variants(source, dest, HOME)
        )

    def test_a_confirmed_cache_becomes_a_rename_to_the_destination_hash(self):
        build_cache(self.projects, SOURCE)
        plan = self.plan()
        self.assertEqual(len(plan.renames), 1)
        rename = plan.renames[0]
        self.assertEqual(rename.old.name, f"api.{cache_suffix(SOURCE)}")
        self.assertEqual(rename.new.name, f"api.{cache_suffix(DEST)}")
        self.assertEqual(rename.new.parent, self.projects)
        self.assertEqual(plan.unconfirmed, [])
        self.assertFalse(plan.relink_required)

    def test_the_reference_count_covers_only_the_recorded_path(self):
        # cache-state.xml holds the one absolute path; the module file is
        # $MODULE_DIR$-relative and contributes nothing.
        build_cache(self.projects, SOURCE)
        self.assertEqual(self.plan().renames[0].references, 1)

    def test_a_cache_that_never_names_the_source_is_left_alone(self):
        cache = self.projects / f"api.{cache_suffix(SOURCE)}"
        cache.mkdir()
        (cache / "cache-state.xml").write_text(
            CACHE_STATE.format(project="/Users/tester/somewhere/else"),
            encoding="utf-8",
        )
        plan = self.plan()
        self.assertEqual(plan.renames, [])
        self.assertEqual(plan.unconfirmed, [cache])

    def test_a_cache_for_a_different_project_is_not_matched(self):
        build_cache(self.projects, HOME / "IdeaProjects" / "other")
        self.assertEqual(self.plan().renames, [])

    def test_the_legacy_external_build_system_layout_is_searched(self):
        legacy = self.caches / "IntelliJIdea2025.3" / "external_build_system"
        legacy.mkdir(parents=True)
        build_cache(legacy, SOURCE)
        plan = self.plan(products=("IntelliJIdea2025.3",))
        self.assertEqual(len(plan.renames), 1)
        self.assertEqual(plan.renames[0].new.parent, legacy)

    def test_every_product_is_searched(self):
        other = self.caches / "IntelliJIdea2026.1" / "projects"
        other.mkdir(parents=True)
        build_cache(self.projects, SOURCE)
        build_cache(other, SOURCE)
        plan = self.plan(products=("IntelliJIdea2026.1", "IntelliJIdea2026.2"))
        self.assertEqual(len(plan.renames), 2)

    def test_a_name_matching_the_source_directory_follows_the_rename(self):
        build_cache(self.projects, SOURCE)
        dest = HOME / "Projects" / "renamed-api"
        plan = self.plan(dest=dest)
        self.assertEqual(plan.renames[0].new.name, f"renamed-api.{cache_suffix(dest)}")

    def test_a_name_the_project_does_not_share_is_kept(self):
        # A project named by .idea/.name keeps that name across a move, so only
        # the hash may change.
        cache = self.projects / f"Display Name.{cache_suffix(SOURCE)}"
        cache.mkdir()
        (cache / "cache-state.xml").write_text(
            CACHE_STATE.format(project=SOURCE.as_posix()), encoding="utf-8"
        )
        dest = HOME / "Projects" / "renamed-api"
        plan = self.plan(dest=dest)
        self.assertEqual(
            plan.renames[0].new.name, f"Display Name.{cache_suffix(dest)}"
        )

    def test_an_existing_destination_is_refused_rather_than_clobbered(self):
        build_cache(self.projects, SOURCE)
        build_cache(self.projects, DEST)
        with self.assertRaises(IdeCacheError) as caught:
            self.plan()
        self.assertIn("already exists", str(caught.exception))

    def test_relink_is_required_when_external_storage_has_no_cache(self):
        source = Path(self.tmp.name) / "api"
        (source / ".idea").mkdir(parents=True)
        (source / ".idea" / "misc.xml").write_text(MISC_EXTERNAL, encoding="utf-8")
        plan = self.plan(source=source)
        self.assertEqual(plan.renames, [])
        self.assertTrue(plan.relink_required)

    def test_relink_is_not_required_when_modules_live_in_the_project(self):
        source = Path(self.tmp.name) / "api"
        (source / ".idea").mkdir(parents=True)
        (source / ".idea" / "misc.xml").write_text(MISC_LOCAL, encoding="utf-8")
        self.assertFalse(self.plan(source=source).relink_required)

    def test_a_missing_caches_root_plans_nothing(self):
        plan = plan_cache_renames(
            Path(self.tmp.name) / "absent", ("IntelliJIdea2026.2",),
            SOURCE, DEST, VARIANTS,
        )
        self.assertEqual(plan.renames, [])
        self.assertEqual(plan.unconfirmed, [])


class TestRenameCaches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_the_directory_moves_and_the_pair_is_reported(self):
        old = build_cache(self.projects, SOURCE)
        new = self.projects / f"api.{cache_suffix(DEST)}"
        performed = rename_caches([CacheRename(old=old, new=new, references=1)])
        self.assertEqual(performed, [(str(old), str(new))])
        self.assertFalse(old.exists())
        self.assertTrue((new / "cache-state.xml").is_file())

    def test_a_vanished_directory_is_skipped_not_raised(self):
        old = self.projects / "gone.deadbeef"
        new = self.projects / "gone.cafebabe"
        with self.assertLogs("idea_migrate.ide_caches", level="WARNING"):
            performed = rename_caches([CacheRename(old=old, new=new, references=1)])
        self.assertEqual(performed, [])


class TestRewriteCaches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir(parents=True)
        self.cache = build_cache(self.projects, SOURCE)
        self.addCleanup(self.tmp.cleanup)

    def test_the_recorded_path_is_repaired(self):
        changed = rewrite_caches([self.cache], VARIANTS)
        state = self.cache / "cache-state.xml"
        self.assertEqual(changed, {str(state): 1})
        text = state.read_text(encoding="utf-8")
        self.assertIn(f"{DEST.as_posix()}/pom.xml", text)
        self.assertNotIn(SOURCE.as_posix(), text)

    def test_module_definitions_are_left_untouched(self):
        module = self.cache / "external_build_system" / "modules" / "api.xml"
        before = module.read_text(encoding="utf-8")
        rewrite_caches([self.cache], VARIANTS)
        self.assertEqual(module.read_text(encoding="utf-8"), before)

    def test_binary_neighbours_are_never_read_or_written(self):
        binary = self.cache / "indexingStamp.bin"
        before = binary.read_bytes()
        rewrite_caches([self.cache], VARIANTS)
        self.assertEqual(binary.read_bytes(), before)
        self.assertNotIn(binary, cache_files(self.cache))

    def test_a_dry_run_counts_without_writing(self):
        state = self.cache / "cache-state.xml"
        before = state.read_text(encoding="utf-8")
        changed = rewrite_caches([self.cache], VARIANTS, dry_run=True)
        self.assertEqual(changed, {str(state): 1})
        self.assertEqual(state.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
