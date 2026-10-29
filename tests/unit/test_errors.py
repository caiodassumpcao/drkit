import pytest

from drkit.errors import (
    CommandError,
    CommandTimeoutError,
    ConfigError,
    DrkitError,
    ExitCode,
    InsecurePermissionsError,
    LockHeldError,
    MissingDependencyError,
    RestoreAbortedError,
)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (DrkitError, ExitCode.PARTIAL_FAILURE),
        (CommandError, ExitCode.PARTIAL_FAILURE),
        (CommandTimeoutError, ExitCode.PARTIAL_FAILURE),
        (ConfigError, ExitCode.CONFIG_ERROR),
        (InsecurePermissionsError, ExitCode.CONFIG_ERROR),
        (LockHeldError, ExitCode.LOCK_HELD),
        (MissingDependencyError, ExitCode.MISSING_DEPENDENCY),
        (RestoreAbortedError, ExitCode.RESTORE_ABORTED),
    ],
)
def test_exit_codes_match_design(error: type[DrkitError], code: ExitCode) -> None:
    assert error("boom").exit_code is code


def test_exit_code_values_are_stable() -> None:
    assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4, 5]


def test_str_includes_hint_when_present() -> None:
    assert str(ConfigError("bad")) == "bad"
    assert str(ConfigError("bad", hint="fix it")) == "bad\nHint: fix it"
