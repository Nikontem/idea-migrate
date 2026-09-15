"""Move IntelliJ's external module storage alongside the directory being migrated.

A JetBrains project normally keeps its module definitions in its own ``.idea``
directory, where they travel with the project. Since external storage became the
default for build-system projects - it is switched on by
``<component name="ExternalStorageConfigurationManager" enabled="true"/>`` in
``.idea/misc.xml`` - they do not. ``modules.xml``, the ``.iml`` files and the
resolved Maven or Gradle project tree live outside the project instead, under::

    ~/Library/Caches/JetBrains/<Product><version>/projects/<name>.<hash>/

and ``<hash>`` is derived from the project's absolute path. Move the project and
the IDE computes a different hash, finds no cache, and opens the project with no
modules at all: the Maven tool window comes up completely empty, with no
Lifecycle, no Plugins, and no project node. It does not re-import on its own.

This is the one thing under ``Library/Caches`` that does not rebuild itself, and
it is why this module exists while the rest of the cache tree is left alone.
Renaming the directory to the hash of the new path, and repairing the one
absolute path recorded inside it, restores the imported model intact.

Three rules keep the rename honest.

**Look the directory up the way the IDE does.** ``<hash>`` is the lower-case hex
of Java's ``String.hashCode()`` over the absolute path, unpadded. Computing it
for the source and globbing for the result is exactly the lookup the IDE
performs, so a directory found this way is the directory the IDE would have
opened. Nothing is decoded out of a directory name, because the hash cannot be
reversed.

**Confirm before renaming.** The hash is an undocumented implementation detail
and, being a 32-bit value over a path, is not collision-proof. A candidate is
therefore only renamed once its ``cache-state.xml`` is shown to mention the
source path, using the same boundary-anchored match the rest of the tool uses. A
candidate that cannot be confirmed is reported and left alone rather than guessed
at; if JetBrains ever changes the derivation, the glob simply matches nothing and
the run says so instead of renaming something at random.

**Say when there is nothing to move.** A project can have external storage
switched on and still have no cache here - most often because it was moved once
already without being reopened in between, which leaves its cache keyed to a path
two generations old. Nothing can be matched in that case, and the user has to
re-link the build system by hand, so :func:`plan_cache_renames` reports it rather
than passing over it in silence.

Only ``*.xml`` files inside a matched directory are rewritten. The binary caches
beside them are regenerable, and editing them as text would corrupt them; the
module definitions under ``external_build_system/`` are already written relative
to ``$MODULE_DIR$`` and need nothing done to them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .errors import IdeCacheError
from .rewrite import read_preserving_newlines, rewrite_file, rewrite_text

logger = logging.getLogger(__name__)

# The two places a product has kept per-project caches. ``projects`` is current;
# ``external_build_system`` is where 2025.3 and earlier put them, and still
# exists on machines that have run both. Both are searched, because which one a
# given directory lives in depends on the version that last wrote it, not on the
# version being migrated for.
CACHE_SUBDIRS = ("projects", "external_build_system")

_EXTERNAL_STORAGE_COMPONENT = "ExternalStorageConfigurationManager"

_HASH_MASK = 0xFFFFFFFF


def java_string_hash(text: str) -> int:
    """Return Java's ``String.hashCode()`` for ``text``, as an unsigned 32-bit int.

    ``h = 31 * h + c`` over the UTF-16 code units, truncated to 32 bits. Python
    strings are iterated by code point rather than code unit, which differs above
    the basic multilingual plane; a path containing such a character would hash
    differently here than in the IDE, and the confirmation step in
    :func:`plan_cache_renames` is what stops that from causing a wrong rename.
    """
    value = 0
    for character in text:
        value = (31 * value + ord(character)) & _HASH_MASK
    return value


def cache_suffix(project: Path) -> str:
    """Return the hash part of the cache directory name for a project path.

    Lower-case hex with no zero padding, because that is what
    ``Integer.toHexString`` produces and therefore what the directory is called.
    """
    return format(java_string_hash(project.as_posix()), "x")


def uses_external_storage(project: Path) -> bool:
    """Say whether a project keeps its module definitions outside ``.idea``.

    Reads the project's own ``.idea/misc.xml``. A missing or unreadable file
    answers False: the question is only asked to decide whether to warn about a
    cache that could not be found, and a warning is not worth raising an error
    over. Malformed XML answers False for the same reason.
    """
    misc = project / ".idea" / "misc.xml"
    try:
        root = ET.fromstring(misc.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ET.ParseError):
        return False

    for component in root.iter("component"):
        if component.get("name") == _EXTERNAL_STORAGE_COMPONENT:
            return component.get("enabled", "").strip().lower() == "true"
    return False


@dataclass(frozen=True)
class CacheRename:
    """One per-project cache directory and the name it must take."""

    old: Path
    new: Path
    # How many references to the old location the directory's XML holds. Counted
    # while confirming the match, and kept so a caller can report the size of
    # what it is about to change without reading the files a second time.
    references: int


@dataclass(frozen=True)
class CachePlan:
    """What the cache step will do, decided before anything is touched."""

    renames: list[CacheRename] = field(default_factory=list)
    # Directories whose name matches the source's hash but whose recorded state
    # never mentions the source path. Either the hash collided or the derivation
    # has changed; renaming on that evidence would be a guess, so they are listed
    # and left alone.
    unconfirmed: list[Path] = field(default_factory=list)
    # True when the project keeps its modules outside .idea but no cache could be
    # matched for it, which means the build system will have to be re-linked by
    # hand after the move. Distinguished from an empty plan, which is the normal
    # and harmless case of a project that keeps its modules in .idea.
    relink_required: bool = False


def _cache_dirs(caches_root: Path, product_names: Sequence[str]) -> list[Path]:
    """Return every directory that holds per-project caches, across products."""
    found: list[Path] = []
    for name in product_names:
        for subdir in CACHE_SUBDIRS:
            directory = caches_root / name / subdir
            if directory.is_dir():
                found.append(directory)
    return found


def cache_files(cache_dir: Path) -> list[Path]:
    """Return the XML files inside one project's cache directory.

    Only XML. The binary caches beside them rebuild themselves, and rewriting one
    as text would corrupt it into something the IDE has to discard anyway.
    """
    return sorted(path for path in cache_dir.rglob("*.xml") if path.is_file())


def _confirm(cache_dir: Path, variants: Sequence[tuple[str, str]]) -> int:
    """Return how many references to the source ``cache_dir`` records.

    Zero means the directory could not be confirmed as this project's. The count
    comes from the same boundary-anchored match used to do the rewriting, so a
    directory is only ever renamed when the rewrite that follows has something to
    do.
    """
    total = 0
    for path in cache_files(cache_dir):
        try:
            text = read_preserving_newlines(path)
        except (OSError, UnicodeDecodeError):
            continue
        _, count = rewrite_text(text, variants)
        total += count
    return total


def plan_cache_renames(
    caches_root: Path,
    product_names: Sequence[str],
    source: Path,
    dest: Path,
    variants: Sequence[tuple[str, str]],
) -> CachePlan:
    """Work out which per-project caches the move requires renaming.

    The source's hash is computed and every product's cache directories are
    globbed for a name ending in it - the lookup the IDE itself does. Each hit is
    then confirmed against its recorded state before it becomes a rename.

    The destination name keeps whatever the matched directory called the project,
    except when that name is the source directory's own basename, in which case it
    becomes the destination's. That covers both spellings the IDE uses without
    having to reimplement how it derives a project's name: a project named after
    its directory follows the rename, and one named by ``.idea/.name`` keeps the
    name the file gives it, which a move does not change.

    A missing caches root is not an error - it means the IDE has never run here.
    Raises :class:`IdeCacheError` if a destination name is already taken, which
    would otherwise make ``os.rename`` clobber a cache the IDE built at the new
    location.
    """
    old_suffix = cache_suffix(source)
    new_suffix = cache_suffix(dest)

    renames: list[CacheRename] = []
    unconfirmed: list[Path] = []

    for directory in _cache_dirs(caches_root, product_names):
        for candidate in sorted(directory.glob(f"*.{old_suffix}")):
            if not candidate.is_dir():
                continue
            references = _confirm(candidate, variants)
            if references == 0:
                unconfirmed.append(candidate)
                continue
            name_part = candidate.name[: -(len(old_suffix) + 1)]
            new_name = name_part if name_part != source.name else dest.name
            renames.append(
                CacheRename(
                    old=candidate,
                    new=directory / f"{new_name}.{new_suffix}",
                    references=references,
                )
            )

    for rename in renames:
        if rename.new.exists() or rename.new.is_symlink():
            raise IdeCacheError(
                f"Cannot rename the IDE cache {rename.old} to {rename.new}: "
                f"that name already exists. The IDE has already built a cache "
                f"for the destination; remove one of the two by hand and run "
                f"again."
            )

    return CachePlan(
        renames=sorted(renames, key=lambda item: str(item.old)),
        unconfirmed=sorted(unconfirmed),
        relink_required=not renames and uses_external_storage(source),
    )


def rename_caches(renames: Sequence[CacheRename]) -> list[tuple[str, str]]:
    """Perform the planned renames. Returns the pairs actually carried out.

    A directory that has disappeared since the plan was made is skipped with a
    warning rather than failing the migration, and is left out of the returned
    list so that undo does not try to reverse a rename that never happened. No
    parent directories are created: a cache directory is renamed within the
    directory it already sits in.
    """
    performed: list[tuple[str, str]] = []
    for rename in renames:
        if not rename.old.exists():
            logger.warning(
                "Skipped renaming the IDE cache %s: it no longer exists.",
                rename.old,
            )
            continue
        os.rename(rename.old, rename.new)
        performed.append((str(rename.old), str(rename.new)))
    return performed


def rewrite_caches(
    cache_dirs: Iterable[Path],
    variants: Sequence[tuple[str, str]],
    dry_run: bool = False,
) -> dict[str, int]:
    """Repair the recorded project path inside each renamed cache directory.

    Returns a mapping of file path to replacement count, listing only files that
    actually changed. Uses the same XML rewriting as the rest of the tool, so a
    change that would produce invalid XML aborts with the file untouched.
    """
    changed: dict[str, int] = {}
    for cache_dir in cache_dirs:
        for path in cache_files(cache_dir):
            count = rewrite_file(path, variants, dry_run=dry_run)
            if count:
                changed[str(path)] = count
    return changed
