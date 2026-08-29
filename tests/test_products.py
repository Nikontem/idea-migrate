import tempfile
import unittest
from pathlib import Path

from idea_migrate.products import find_product_dirs


def build_jetbrains_root(base: Path) -> Path:
    """Build a synthetic JetBrains configuration root."""
    root = base / "JetBrains"
    for name in ("IntelliJIdea2026.2", "PyCharm2026.2", "WebStorm2025.3"):
        (root / name / "options").mkdir(parents=True)
        (root / name / "workspace").mkdir(parents=True)
    # Support directories with no options/ subdirectory - must be ignored.
    (root / "Toolbox").mkdir(parents=True)
    (root / "consentOptions").mkdir(parents=True)
    (root / "PrivacyPolicy" / "nested").mkdir(parents=True)
    # A stray file at the top level - must not crash the scan.
    (root / "stray.txt").write_text("ignore me", encoding="utf-8")
    return root


class TestFindProductDirs(unittest.TestCase):
    def test_finds_only_dirs_containing_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            found = [p.name for p in find_product_dirs(root)]
            self.assertEqual(
                found, ["IntelliJIdea2026.2", "PyCharm2026.2", "WebStorm2025.3"]
            )

    def test_results_are_sorted_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            found = [p.name for p in find_product_dirs(root)]
            self.assertEqual(found, sorted(found))

    def test_exclude_removes_named_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            found = [p.name for p in find_product_dirs(root, exclude=["PyCharm2026.2"])]
            self.assertEqual(found, ["IntelliJIdea2026.2", "WebStorm2025.3"])

    def test_missing_root_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(find_product_dirs(Path(tmp) / "absent"), [])

    def test_returns_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            for product in find_product_dirs(root):
                self.assertTrue(product.is_absolute())
                self.assertTrue((product / "options").is_dir())


if __name__ == "__main__":
    unittest.main()
