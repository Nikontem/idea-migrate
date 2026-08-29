"""Exception hierarchy for idea-migrate.

Every error the tool raises deliberately derives from MigrateError so the
command-line layer can catch one class and print a clean message instead of a
traceback.
"""


class MigrateError(Exception):
    """Base class for every error this tool raises deliberately."""


class PathValidationError(MigrateError):
    """The source or destination path is unusable."""


class IdeRunningError(MigrateError):
    """A JetBrains IDE is running, so its configuration must not be touched."""


class XmlIntegrityError(MigrateError):
    """Rewriting a file would have produced invalid XML."""


class BackupError(MigrateError):
    """The backup could not be created."""


class UndoError(MigrateError):
    """A backup could not be rolled back."""
