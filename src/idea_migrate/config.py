"""Configuration: where backups go and where the IDEs keep their settings.

Every setting has a working default, so the TOML file is optional.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

BACKUP_DIR_NAME = "Idea-Migration-Backups"


@dataclass(frozen=True)
class Config:
    backup_root: Path
    jetbrains_root: Path
    # Claude Code keeps one directory per working directory under
    # <claude_root>/projects, so a move has to be reflected there too.
    claude_root: Path
    # Claude Code also keeps one registry file listing every working directory it
    # has been started in, keyed by absolute path; a move has to be reflected
    # there as well. It sits beside the directory above rather than inside it,
    # so it gets its own setting.
    claude_registry: Path
    # Where the IDEs keep their per-project caches. Separate from
    # jetbrains_root because the two trees are siblings in name only: this one
    # holds regenerable data, and only the external module storage inside it is
    # ever touched. See ide_caches for why that one part is not regenerable.
    caches_root: Path
    exclude_products: tuple[str, ...]


def default_config(home: Path) -> Config:
    """Return the configuration used when no file overrides anything."""
    return Config(
        backup_root=home / BACKUP_DIR_NAME,
        jetbrains_root=home / "Library" / "Application Support" / "JetBrains",
        claude_root=home / ".claude",
        claude_registry=home / ".claude.json",
        caches_root=home / "Library" / "Caches" / "JetBrains",
        exclude_products=(),
    )


def load_config(path: Path | None, home: Path) -> Config:
    """Load configuration from a TOML file, falling back to defaults.

    A missing file is not an error - the file is optional. Keys absent from the
    file keep their default values.
    """
    config = default_config(home)
    if path is None or not path.is_file():
        return config

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    changes: dict[str, object] = {}
    if "backup_root" in data:
        changes["backup_root"] = Path(data["backup_root"]).expanduser()
    if "jetbrains_root" in data:
        changes["jetbrains_root"] = Path(data["jetbrains_root"]).expanduser()
    if "claude_root" in data:
        changes["claude_root"] = Path(data["claude_root"]).expanduser()
    if "claude_registry" in data:
        changes["claude_registry"] = Path(data["claude_registry"]).expanduser()
    if "caches_root" in data:
        changes["caches_root"] = Path(data["caches_root"]).expanduser()
    if "exclude_products" in data:
        changes["exclude_products"] = tuple(data["exclude_products"])

    return replace(config, **changes)
