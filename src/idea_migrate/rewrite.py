"""Rewrite stored path references inside JetBrains configuration files.

This is the highest-risk part of the tool, so three rules govern it.

Boundary anchoring: a prefix matches only when the next character ends the path
component - a separator, a quote, an angle bracket, or end of string. Without
this, moving "WebstormProjects" would also corrupt "WebstormProjectsArchive".

Case-insensitive matching: the filesystem is case-insensitive, so different IDEs
may have recorded one directory under different spellings. A case-sensitive
search would silently miss half the references and report success. Only the
matched prefix is replaced; every byte after it is preserved.

Verify then write: editing is done on raw text, never by re-serializing the XML
tree, because that would reorder attributes and reflow the entire file. The
result is parsed to confirm it is still well-formed before it is written. A file
that was already malformed before we touched it is skipped, not blamed on us.
"""

from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path

from .errors import XmlIntegrityError

USER_HOME_MACRO = "$USER_HOME$"

# The prefix must be followed by something that ends the path component.
_BOUNDARY = r"""(?=[/"'<]|$)"""


def _macro_form(path: Path, home: Path) -> str | None:
    """Return the ``$USER_HOME$``-relative form of a path, or None if not under home."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        return None
    return f"{USER_HOME_MACRO}/{relative.as_posix()}"


def prefix_variants(old: Path, new: Path, home: Path) -> list[tuple[str, str]]:
    """Return every (old, new) prefix pair to search for.

    JetBrains writes paths in more than one form: absolute, relative to the
    ``$USER_HOME$`` placeholder, and as ``file://`` URLs of either.
    """
    variants: list[tuple[str, str]] = [(old.as_posix(), new.as_posix())]

    old_macro = _macro_form(old, home)
    new_macro = _macro_form(new, home)
    if old_macro and new_macro:
        variants.append((old_macro, new_macro))

    for old_form, new_form in list(variants):
        variants.append((f"file://{old_form}", f"file://{new_form}"))

    return variants


def rewrite_text(
    text: str, variants: Sequence[tuple[str, str]]
) -> tuple[str, int]:
    """Replace every anchored occurrence of each old prefix. Returns text and count."""
    total = 0
    for old, new in variants:
        pattern = re.compile(re.escape(old) + _BOUNDARY, re.IGNORECASE)
        # A lambda keeps the replacement literal - a backslash or \g in a path
        # would otherwise be read as a backreference.
        text, count = pattern.subn(lambda _match, value=new: value, text)
        total += count
    return text, total


def config_files(product_dir: Path) -> list[Path]:
    """Return the XML files under a product's options/ and workspace/ directories."""
    found: list[Path] = []
    for subdir in ("options", "workspace"):
        directory = product_dir / subdir
        if directory.is_dir():
            found.extend(sorted(directory.glob("*.xml")))
    return found


def rewrite_file(
    path: Path, variants: Sequence[tuple[str, str]], dry_run: bool = False
) -> int:
    """Rewrite one file in place. Returns the number of replacements made.

    Returns 0 without writing when nothing matched or when the file was already
    malformed. Raises XmlIntegrityError if the rewrite would produce invalid XML,
    leaving the original untouched.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    try:
        ET.fromstring(original)
    except ET.ParseError:
        # Already broken before we arrived; not ours to fix or to blame.
        return 0

    updated, count = rewrite_text(original, variants)
    if count == 0:
        return 0

    try:
        ET.fromstring(updated)
    except ET.ParseError as exc:
        raise XmlIntegrityError(
            f"Rewriting {path} would have produced invalid XML: {exc}"
        ) from exc

    if dry_run:
        return count

    mode = path.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    )
    try:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
    os.chmod(path, mode)
    return count


def rewrite_products(
    product_dirs: Iterable[Path],
    variants: Sequence[tuple[str, str]],
    dry_run: bool = False,
) -> dict[str, int]:
    """Rewrite every configuration file under each product directory.

    Returns a mapping of file path to replacement count, listing only files that
    actually changed.
    """
    changed: dict[str, int] = {}
    for product_dir in product_dirs:
        for file_path in config_files(product_dir):
            count = rewrite_file(file_path, variants, dry_run=dry_run)
            if count:
                changed[str(file_path)] = count
    return changed


def count_references(
    product_dirs: Iterable[Path], variants: Sequence[tuple[str, str]]
) -> int:
    """Count how many references to the old prefixes still exist."""
    total = 0
    for product_dir in product_dirs:
        for file_path in config_files(product_dir):
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            _, count = rewrite_text(text, variants)
            total += count
    return total
