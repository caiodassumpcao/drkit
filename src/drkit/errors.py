"""Exception hierarchy. Every error a user can hit maps to a documented exit code (DESIGN §10)."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    PARTIAL_FAILURE = 1
    CONFIG_ERROR = 2
    LOCK_HELD = 3
    MISSING_DEPENDENCY = 4
    RESTORE_ABORTED = 5


class DrkitError(Exception):
    """Base class for expected, user-facing failures.

    ``message`` says what failed; ``hint`` says how to fix it. Both must be safe to print:
    callers are responsible for never embedding secret values.
    """

    exit_code: ExitCode = ExitCode.PARTIAL_FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message if self.hint is None else f"{self.message}\nHint: {self.hint}"


class ConfigError(DrkitError):
    """The manifest or one of its referenced files is missing, invalid or unreadable."""

    exit_code = ExitCode.CONFIG_ERROR


class InsecurePermissionsError(ConfigError):
    """A file holding secrets is accessible by users other than its owner."""


class LockHeldError(DrkitError):
    """Another drkit process holds the run lock."""

    exit_code = ExitCode.LOCK_HELD


class MissingDependencyError(DrkitError):
    """A required external binary is missing or too old."""

    exit_code = ExitCode.MISSING_DEPENDENCY


class CommandError(DrkitError):
    """An external command failed or timed out."""


class CommandTimeoutError(CommandError):
    """An external command exceeded its timeout and was killed."""


class RestoreAbortedError(DrkitError):
    """A restore stopped before completing (declined confirmation or a failed step)."""

    exit_code = ExitCode.RESTORE_ABORTED
