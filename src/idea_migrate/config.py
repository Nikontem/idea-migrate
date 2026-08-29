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
    exclude_products: tuple[str, ...]


def default_config(home: Path) -> Config:
    """Return the configuration used when no file overrides anything."""
    return Config(
        backup_root=home / BACKUP_DIR_NAME,
        jetbrains_root=home / "Library" / "Application Support" / "JetBrains",
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
    if "exclude_products" in data:
        changes["exclude_products"] = tuple(data["exclude_products"])

    return replace(config, **changes)
