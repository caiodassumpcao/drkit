import os
import subprocess
import sys
from pathlib import Path

import pytest

from drkit import lock
from drkit.errors import ConfigError, LockHeldError


def test_acquire_writes_pid_and_clears_it_on_release(tmp_path: Path) -> None:
    path = tmp_path / "drkit.lock"
    with lock.acquire(path):
        assert path.read_text() == f"{os.getpid()}\n"
        assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == ""


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    path = tmp_path / "drkit.lock"
    with lock.acquire(path):
        pass
    with lock.acquire(path):
        pass


def test_second_holder_is_rejected_with_pid(tmp_path: Path) -> None:
    path = tmp_path / "drkit.lock"
    with (
        lock.acquire(path),
        pytest.raises(LockHeldError, match=rf"pid {os.getpid()}"),
        lock.acquire(path),
    ):
        pass  # pragma: no cover


def test_lock_held_by_another_process(tmp_path: Path) -> None:
    path = tmp_path / "drkit.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from drkit import lock\n"
            "with lock.acquire(__import__('pathlib').Path(sys.argv[1])):\n"
            "    print('locked', flush=True); time.sleep(30)",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(LockHeldError) as excinfo, lock.acquire(path):
            pass  # pragma: no cover
        assert excinfo.value.exit_code == 3
        assert f"pid {holder.pid}" in excinfo.value.message
    finally:
        holder.kill()
        holder.wait()

    # The kernel released the lock when the holder died.
    with lock.acquire(path):
        pass


def test_unwritable_location_is_a_config_error(tmp_path: Path) -> None:
    with (
        pytest.raises(ConfigError, match="cannot open lock file"),
        lock.acquire(tmp_path / "missing-dir" / "drkit.lock"),
    ):
        pass  # pragma: no cover


def test_lock_path_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DRKIT_LOCK_FILE", raising=False)
    assert lock.lock_path() == Path("/run/drkit.lock")
    monkeypatch.setenv("DRKIT_LOCK_FILE", str(tmp_path / "custom.lock"))
    with lock.acquire():
        assert (tmp_path / "custom.lock").read_text() == f"{os.getpid()}\n"
