import tempfile
import unittest
from pathlib import Path

from idea_migrate.config import Config, default_config, load_config


class TestDefaultConfig(unittest.TestCase):
    def test_defaults_derive_from_home(self):
        home = Path("/fake/home")
        cfg = default_config(home)
        self.assertEqual(cfg.backup_root, home / "Idea-Migration-Backups")
        self.assertEqual(
            cfg.jetbrains_root,
            home / "Library" / "Application Support" / "JetBrains",
        )
        self.assertEqual(cfg.claude_root, home / ".claude")
        self.assertEqual(cfg.claude_registry, home / ".claude.json")
        self.assertEqual(cfg.exclude_products, ())

    def test_config_is_frozen(self):
        cfg = default_config(Path("/fake/home"))
        with self.assertRaises(Exception):
            cfg.backup_root = Path("/elsewhere")


class TestLoadConfig(unittest.TestCase):
    def test_none_path_returns_defaults(self):
        home = Path("/fake/home")
        self.assertEqual(load_config(None, home), default_config(home))

    def test_missing_file_returns_defaults(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.toml"
            self.assertEqual(load_config(missing, home), default_config(home))

    def test_file_overrides_defaults(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(
                'backup_root = "/custom/backups"\n'
                'exclude_products = ["Toolbox", "Air"]\n',
                encoding="utf-8",
            )
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.backup_root, Path("/custom/backups"))
            self.assertEqual(cfg.exclude_products, ("Toolbox", "Air"))
            # Unset keys keep their defaults.
            self.assertEqual(
                cfg.jetbrains_root,
                home / "Library" / "Application Support" / "JetBrains",
            )
            self.assertEqual(cfg.claude_root, home / ".claude")
            self.assertEqual(cfg.claude_registry, home / ".claude.json")

    def test_claude_root_can_be_overridden(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(
                'claude_root = "/custom/claude"\n', encoding="utf-8"
            )
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.claude_root, Path("/custom/claude"))

    def test_tilde_in_claude_root_is_expanded(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text('claude_root = "~/.claude-alt"\n', encoding="utf-8")
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.claude_root, Path.home() / ".claude-alt")

    def test_claude_registry_can_be_overridden(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(
                'claude_registry = "/custom/claude.json"\n', encoding="utf-8"
            )
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.claude_registry, Path("/custom/claude.json"))
            # The registry file and the data directory are set independently.
            self.assertEqual(cfg.claude_root, home / ".claude")

    def test_tilde_in_claude_registry_is_expanded(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(
                'claude_registry = "~/.claude-alt.json"\n', encoding="utf-8"
            )
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.claude_registry, Path.home() / ".claude-alt.json")

    def test_tilde_in_config_is_expanded(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text('backup_root = "~/my-backups"\n', encoding="utf-8")
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.backup_root, Path.home() / "my-backups")


if __name__ == "__main__":
    unittest.main()
