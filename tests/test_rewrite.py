import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from idea_migrate.errors import XmlIntegrityError
from idea_migrate.rewrite import (
    config_files,
    count_references,
    prefix_variants,
    rewrite_file,
    rewrite_products,
    rewrite_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
HOME = Path("/Users/tester")
OLD = HOME / "WebstormProjects"
NEW = HOME / "Projects" / "WebstormProjects"


class TestPrefixVariants(unittest.TestCase):
    def test_produces_placeholder_and_expanded_forms(self):
        variants = dict(prefix_variants(OLD, NEW, HOME))
        self.assertEqual(
            variants["$USER_HOME$/WebstormProjects"],
            "$USER_HOME$/Projects/WebstormProjects",
        )
        self.assertEqual(
            variants["/Users/tester/WebstormProjects"],
            "/Users/tester/Projects/WebstormProjects",
        )

    def test_produces_file_url_forms(self):
        variants = dict(prefix_variants(OLD, NEW, HOME))
        self.assertEqual(
            variants["file:///Users/tester/WebstormProjects"],
            "file:///Users/tester/Projects/WebstormProjects",
        )
        self.assertEqual(
            variants["file://$USER_HOME$/WebstormProjects"],
            "file://$USER_HOME$/Projects/WebstormProjects",
        )

    def test_path_outside_home_has_no_placeholder_variant(self):
        outside = Path("/opt/work")
        variants = dict(prefix_variants(outside, Path("/opt/moved"), HOME))
        # Only the absolute form and its file:// URL: a path outside the home
        # directory has no $USER_HOME$ spelling at all.
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants["/opt/work"], "/opt/moved")
        self.assertEqual(variants["file:///opt/work"], "file:///opt/moved")


class TestRewriteText(unittest.TestCase):
    def setUp(self):
        self.variants = prefix_variants(OLD, NEW, HOME)

    def test_replaces_placeholder_form(self):
        text = '<entry key="$USER_HOME$/WebstormProjects/alpha">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(
            result, '<entry key="$USER_HOME$/Projects/WebstormProjects/alpha">'
        )
        self.assertEqual(count, 1)

    def test_does_not_match_longer_sibling_directory(self):
        text = '<entry key="$USER_HOME$/WebstormProjectsArchive/beta">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_does_not_match_a_sibling_whose_extra_word_follows_a_space(self):
        # A space is legal inside a macOS directory name, so it must not end a
        # path component: "WebstormProjects Archive" is a different directory.
        text = '<entry key="$USER_HOME$/WebstormProjects Archive/beta">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_does_not_match_a_sibling_that_is_a_spaced_suffix_of_the_source(self):
        variants = prefix_variants(HOME / "Projects", HOME / "Moved", HOME)
        text = '<entry key="/Users/tester/Projects 2024/x">'
        result, count = rewrite_text(text, variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_does_not_match_a_sibling_whose_next_character_is_a_greater_than(self):
        # ">" is legal in a macOS filename and XML does not require it to be
        # escaped, so it cannot end a path component. See _BOUNDARY.
        variants = prefix_variants(HOME / "Projects", HOME / "Moved", HOME)
        text = '<entry key="/Users/tester/Projects>2024/x">'
        result, count = rewrite_text(text, variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_matches_a_different_capitalization(self):
        text = '<entry key="/Users/tester/webstormprojects/gamma">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(
            result, '<entry key="/Users/tester/Projects/WebstormProjects/gamma">'
        )
        self.assertEqual(count, 1)

    def test_matches_bare_prefix_before_closing_quote(self):
        text = '<option value="$USER_HOME$/WebstormProjects" />'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(
            result, '<option value="$USER_HOME$/Projects/WebstormProjects" />'
        )
        self.assertEqual(count, 1)

    def test_leaves_unrelated_paths_alone(self):
        text = '<entry key="$USER_HOME$/Downloads/delta">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_matches_bare_path_in_a_text_node_before_a_newline(self):
        text = "<a>$USER_HOME$/WebstormProjects\n</a>"
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(result, "<a>$USER_HOME$/Projects/WebstormProjects\n</a>")
        self.assertEqual(count, 1)

    def test_destination_nested_under_source_is_rewritten_only_once(self):
        source = HOME / "Projects"
        nested = source / "Sub"
        variants = prefix_variants(source, nested, HOME)
        text = '<a k="file:///Users/tester/Projects/x" />'
        result, count = rewrite_text(text, variants)
        self.assertEqual(result, '<a k="file:///Users/tester/Projects/Sub/x" />')
        self.assertEqual(count, 1)

    def test_replacement_is_literal_not_a_backreference(self):
        odd_new = Path("/Users/tester/a\\1b")
        variants = prefix_variants(OLD, odd_new, HOME)
        text = '<entry key="/Users/tester/WebstormProjects/x">'
        result, count = rewrite_text(text, variants)
        self.assertEqual(result, '<entry key="/Users/tester/a\\1b/x">')
        self.assertEqual(count, 1)

    def test_case_folding_codepoint_does_not_crash(self):
        # re.IGNORECASE treats a long s (U+017F) as equal to "s", but
        # str.lower() does not, so selecting the replacement by lower-casing the
        # matched text used to raise KeyError here.
        variants = prefix_variants(HOME / "Assets", HOME / "Moved" / "Assets", HOME)
        text = '<a k="/Users/tester/A\u017Fsets/x" />'
        result, count = rewrite_text(text, variants)
        self.assertEqual(result, '<a k="/Users/tester/Moved/Assets/x" />')
        self.assertEqual(count, 1)


class TestRewriteFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.variants = prefix_variants(OLD, NEW, HOME)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rewrites_and_stays_valid_xml(self):
        target = self.tmp / "recentProjects.xml"
        shutil.copy(FIXTURES / "recentProjects.xml", target)
        count = rewrite_file(target, self.variants)
        self.assertEqual(count, 2)  # placeholder form + lowercase expanded form
        text = target.read_text(encoding="utf-8")
        self.assertIn("$USER_HOME$/Projects/WebstormProjects/alpha", text)
        self.assertIn("$USER_HOME$/WebstormProjectsArchive/beta", text)
        self.assertIn("$USER_HOME$/Downloads/delta", text)
        ET.fromstring(text)  # raises if malformed

    def test_dry_run_reports_but_does_not_write(self):
        target = self.tmp / "recentProjects.xml"
        shutil.copy(FIXTURES / "recentProjects.xml", target)
        before = target.read_bytes()
        count = rewrite_file(target, self.variants, dry_run=True)
        self.assertEqual(count, 2)
        self.assertEqual(target.read_bytes(), before)

    def test_already_malformed_file_is_skipped_with_a_warning(self):
        target = self.tmp / "malformed.xml"
        shutil.copy(FIXTURES / "malformed.xml", target)
        before = target.read_bytes()
        with self.assertLogs("idea_migrate.rewrite", level="WARNING") as captured:
            count = rewrite_file(target, self.variants)
        self.assertEqual(count, 0)
        self.assertEqual(target.read_bytes(), before)
        # A silent skip would be indistinguishable from a file with nothing to
        # change, so the caller has to be told the file was left alone.
        self.assertEqual(len(captured.output), 1)
        self.assertIn("malformed.xml", captured.output[0])
        self.assertIn("already not well-formed", captured.output[0])

    def test_unreadable_file_is_skipped_with_a_warning(self):
        target = self.tmp / "missing" / "gone.xml"
        with self.assertLogs("idea_migrate.rewrite", level="WARNING") as captured:
            count = rewrite_file(target, self.variants)
        self.assertEqual(count, 0)
        self.assertIn("could not be read", captured.output[0])

    def test_non_utf8_file_is_skipped_with_a_warning(self):
        target = self.tmp / "latin1.xml"
        target.write_bytes(b'<a k="\xff\xfe/WebstormProjects" />')
        with self.assertLogs("idea_migrate.rewrite", level="WARNING") as captured:
            count = rewrite_file(target, self.variants)
        self.assertEqual(count, 0)
        self.assertIn("not valid UTF-8", captured.output[0])

    def test_rewrite_that_would_break_xml_raises(self):
        target = self.tmp / "recentProjects.xml"
        shutil.copy(FIXTURES / "recentProjects.xml", target)
        before = target.read_bytes()
        breaking = [("$USER_HOME$/WebstormProjects", 'x"><broken')]
        with self.assertRaises(XmlIntegrityError):
            rewrite_file(target, breaking)
        self.assertEqual(target.read_bytes(), before)

    def test_crlf_line_endings_are_preserved(self):
        target = self.tmp / "crlf.xml"
        target.write_bytes(
            b"<application>\r\n"
            b'  <entry key="$USER_HOME$/WebstormProjects/alpha" />\r\n'
            b"</application>\r\n"
        )
        count = rewrite_file(target, self.variants)
        self.assertEqual(count, 1)
        data = target.read_bytes()
        self.assertEqual(data.count(b"\r\n"), 3)
        self.assertNotIn(b"\r\r", data)
        self.assertIn(b'"$USER_HOME$/Projects/WebstormProjects/alpha"', data)

    def test_file_with_no_matches_is_untouched(self):
        target = self.tmp / "other.xml"
        target.write_text('<a key="$USER_HOME$/Downloads/x" />', encoding="utf-8")
        before = target.stat().st_mtime_ns
        self.assertEqual(rewrite_file(target, self.variants), 0)
        self.assertEqual(target.stat().st_mtime_ns, before)


class TestProductScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.product = Path(self._tmp.name) / "IntelliJIdea2026.2"
        (self.product / "options").mkdir(parents=True)
        (self.product / "workspace").mkdir(parents=True)
        shutil.copy(
            FIXTURES / "recentProjects.xml",
            self.product / "options" / "recentProjects.xml",
        )
        (self.product / "workspace" / "AAA.xml").write_text(
            '<project><path value="$USER_HOME$/WebstormProjects/alpha" /></project>',
            encoding="utf-8",
        )
        (self.product / "options" / "notes.txt").write_text("x", encoding="utf-8")
        self.variants = prefix_variants(OLD, NEW, HOME)

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_files_finds_only_xml_in_options_and_workspace(self):
        names = sorted(p.name for p in config_files(self.product))
        self.assertEqual(names, ["AAA.xml", "recentProjects.xml"])

    def test_count_references_before_rewriting(self):
        self.assertEqual(count_references([self.product], self.variants), 3)

    def test_rewrite_products_reports_per_file_counts(self):
        result = rewrite_products([self.product], self.variants)
        self.assertEqual(sum(result.values()), 3)
        self.assertEqual(count_references([self.product], self.variants), 0)


if __name__ == "__main__":
    unittest.main()
