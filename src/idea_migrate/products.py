"""Find the configuration directories of installed JetBrains products.

A directory counts as a product configuration directory when it contains an
``options`` subdirectory. That structural test is deliberate: it picks up newly
installed products and versions without a hardcoded name list, and it skips
support directories such as Toolbox, consentOptions, and PrivacyPolicy.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def find_product_dirs(
    jetbrains_root: Path, exclude: Sequence[str] = ()
) -> list[Path]:
    """Return every product configuration directory under ``jetbrains_root``.

    Results are absolute and sorted by directory name. A missing root is not an
    error - it simply means no JetBrains products are installed.
    """
    if not jetbrains_root.is_dir():
        return []

    excluded = set(exclude)
    products = [
        entry
        for entry in jetbrains_root.iterdir()
        if entry.is_dir()
        and entry.name not in excluded
        and (entry / "options").is_dir()
    ]
    return sorted((p.resolve() for p in products), key=lambda p: p.name)
