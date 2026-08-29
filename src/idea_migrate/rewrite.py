"""Rewrite stored path references inside JetBrains configuration files.

This is the highest-risk part of the tool, so four rules govern it.

Boundary anchoring: a prefix matches only when the next character ends the path
component - a separator, a quote, an angle bracket, a line break or tab, or end
of string. Without this, moving "WebstormProjects" would also corrupt
"WebstormProjectsArchive". The space character is deliberately NOT a boundary:
spaces are legal inside macOS directory names, so accepting one would make
"Projects" match the start of "Projects 2024" and silently break a reference to
a directory the user never moved.

One pass: every prefix goes into a single alternation rather than a substitution
each, so no byte can be rewritten twice by a later variant matching an earlier
variant's output.

Case-insensitive matching: the filesystem is case-insensitive, so different IDEs
may have recorded one directory under different spellings. A case-sensitive
search would silently miss half the references and report success. Only the
matched prefix is replaced; every byte after it is preserved.

Verify then write: editing is done on raw text, never by re-serializing the XML
tree, because that would reorder attributes and reflow the entire file. The
result is parsed to confirm it is still well-formed before it is written. A file
that was already malformed before we touched it is skipped with a warning, not
blamed on us.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path

from .errors import XmlIntegrityError

logger = logging.getLogger(__name__)

USER_HOME_MACRO = "$USER_HOME$"

# The prefix must be followed by something that ends the path component. A
# space is not in this set on purpose - see the module docstring.
_BOUNDARY = r"""(?=[/"'<>\r\n\t]|$)"""


def _macro_form(path: Path, home: Path) -> str | None:
    """Return the ``$USER_HOME$``-relative form of a path, or None if not under home."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        return None
    return f"{USER_HOME_MACRO}/{relative.as_posix()}"


def _read_preserving_newlines(path: Path) -> str:
    """Read a file without translating its line endings.

    ``newline=""`` turns off universal-newline translation, so a file written
    with CRLF endings still reads back with its carriage returns intact. Without
    it a rewrite would quietly convert the whole file to LF, which breaks the
    promise that only the matched prefix changes.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


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
    """Replace every anchored occurrence of each old prefix, in one pass.

    One alternation over all the prefixes is used rather than one substitution
    per prefix, so text that has already been rewritten can never be matched
    again by a later variant. Substituting variant by variant meant a
    destination nested under the source had its new tail appended twice. The
    longest prefixes are tried first, so where two overlap the most specific one
    wins. Returns the new text and the number of replacements made.
    """
    ordered = sorted(variants, key=lambda pair: len(pair[0]), reverse=True)
    if not ordered:
        return text, 0

    # Each alternative gets its own named group, and the replacement is chosen
    # by which group matched. Looking the matched text up by its lowercase form
    # instead would crash on the codepoints where re.IGNORECASE and str.lower
    # disagree - a long s matches "s" but does not lower-case to it - and would
    # also collapse two variants that differ only in case.
    replacements = {f"v{index}": new for index, (_, new) in enumerate(ordered)}
    pattern = re.compile(
        # The non-capturing wrapper matters: the boundary lookahead has to apply
        # to the whole alternation, not only to its last branch.
        "(?:"
        + "|".join(
            f"(?P<v{index}>{re.escape(old)})" for index, (old, _) in enumerate(ordered)
        )
        + ")"
        + _BOUNDARY,
        re.IGNORECASE,
    )
    # A lambda keeps the replacement literal - a backslash or \g in a path
    # would otherwise be read as a backreference.
    return pattern.subn(lambda match: replacements[match.lastgroup], text)


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

    Returns 0 without writing when nothing matched, when the file could not be
    read, or when it was already malformed; the last three are logged as
    warnings so a skipped file is never mistaken for a clean one. Raises
    XmlIntegrityError if the rewrite would produce invalid XML, leaving the
    original untouched.
    """
    try:
        original = _read_preserving_newlines(path)
    except UnicodeDecodeError:
        logger.warning("Skipped %s: it is not valid UTF-8 text.", path)
        return 0
    except OSError as exc:
        logger.warning("Skipped %s: it could not be read (%s).", path, exc)
        return 0

    try:
        ET.fromstring(original)
    except ET.ParseError as exc:
        # Already broken before we arrived; not ours to fix or to blame. Say so,
        # so the caller cannot mistake this for a file with nothing to change.
        logger.warning(
            "Skipped %s: it was already not well-formed XML before this run (%s).",
            path,
            exc,
        )
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

    mode = stat.S_IMODE(path.stat().st_mode)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    )
    temp_name = handle.name
    try:
        try:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temp_name, path)
    except BaseException:
        # Never leave a stray temporary file behind in the user's config dir.
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise
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
                text = _read_preserving_newlines(file_path)
            except UnicodeDecodeError:
                logger.warning(
                    "Not counted: %s is not valid UTF-8 text.", file_path
                )
                continue
            except OSError as exc:
                logger.warning(
                    "Not counted: %s could not be read (%s).", file_path, exc
                )
                continue
            _, count = rewrite_text(text, variants)
            total += count
    return total
