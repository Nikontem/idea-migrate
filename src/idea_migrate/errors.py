"""Exception hierarchy for idea-migrate.

Every error the tool raises deliberately derives from MigrateError so the
command-line layer can catch one class and print a clean message instead of a
traceback.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    # Imported for the annotation on RunFailed.state alone. A real import here
    # would be circular: batch.py raises these errors and therefore imports
    # this module.
    from .batch import RunState


class MigrateError(Exception):
    """Base class for every error this tool raises deliberately."""


class PathValidationError(MigrateError):
    """The source or destination path is unusable."""


class IdeRunningError(MigrateError):
    """A JetBrains IDE is running, so its configuration must not be touched."""


class XmlIntegrityError(MigrateError):
    """Rewriting a file would have produced invalid XML."""


class JsonIntegrityError(MigrateError):
    """Rewriting a file would have produced invalid JSON."""


class ClaudeDataError(MigrateError):
    """Claude Code project data could not be relocated."""


class IdeCacheError(MigrateError):
    """An IDE's per-project cache could not be relocated."""


class BackupError(MigrateError):
    """The backup could not be created."""


class MoveError(MigrateError):
    """The directory could not be moved, or the copy could not be verified."""


class UndoError(MigrateError):
    """A backup could not be rolled back."""


class PlanError(MigrateError):
    """A batch of moves was refused before anything was touched.

    Every reason found is kept, in ``problems``, rather than only the first.
    A caller planning several moves at once wants to fix all of them in one
    pass instead of rediscovering the next one on every attempt; the message
    is the reasons joined by newlines, so a batch of one still prints exactly
    the single line its one problem would have produced on its own.
    """

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


class RunFailed(MigrateError):
    """A move failed part-way through a run that had already started.

    ``cause`` is the error that actually stopped the run and ``state`` says how
    far the run had got - whether a backup exists, which moves completed, and
    whether the failing move's directory had already left its source.

    The message is the cause's own message, unchanged, so a caller that prints
    the error prints what the underlying failure said rather than a wrapper's
    paraphrase of it. The recovery advice belongs to the caller, which is the
    only party that knows how it wants to say it.
    """

    def __init__(self, cause: BaseException, state: RunState):
        self.cause = cause
        self.state = state
        super().__init__(str(cause))
