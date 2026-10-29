import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drkit import state
from drkit.errors import ConfigError
from drkit.runmanifest import RunManifest

T0 = datetime(2026, 10, 27, 3, tzinfo=UTC)


def manifest(run_id: str, status: str) -> RunManifest:
    return RunManifest.model_validate(
        {
            "run_id": run_id,
            "host": "h",
            "drkit_version": "0.1.0",
            "started_at": T0,
            "finished_at": T0,
            "status": status,
        }
    )


def test_missing_file_is_empty_state(tmp_path: Path) -> None:
    assert state.load(tmp_path) == state.State()


def test_record_runs_tracks_last_success(tmp_path: Path) -> None:
    directory = tmp_path / "drkit"
    state.record_run(manifest("r1", "ok"), directory)
    current = state.record_run(manifest("r2", "partial"), directory)

    assert current.last_run is not None
    assert current.last_run.run_id == "r2"
    assert current.last_success is not None
    assert current.last_success.run_id == "r1"
    assert state.load(directory) == current
    assert directory.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in directory.iterdir()] == ["state.json"]  # no temp files left


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({"last_drill": {"rto_s": 300}}))
    assert state.load(tmp_path) == state.State()


@pytest.mark.parametrize("content", ["{not json", '{"last_run": {"run_id": 1}}'])
def test_corrupt_file(tmp_path: Path, content: str) -> None:
    (tmp_path / "state.json").write_text(content)
    with pytest.raises(ConfigError, match="cannot read state file"):
        state.load(tmp_path)


def test_unwritable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(ConfigError, match="cannot write state file") as excinfo:
        state.save(state.State(), blocker / "sub")
    assert excinfo.value.hint is not None
    assert "DRKIT_STATE_DIR" in excinfo.value.hint


def test_state_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DRKIT_STATE_DIR", raising=False)
    assert state.state_dir() == Path("/var/lib/drkit")
    monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path))
    assert state.state_dir() == tmp_path
    state.save(state.State())
    assert (tmp_path / "state.json").exists()


def test_record_verify_keeps_the_run_records(tmp_path: Path) -> None:
    state.record_run(manifest("r1", "ok"), tmp_path)
    current = state.record_verify("r1", "failed", tmp_path)
    assert current.last_verify is not None
    assert (current.last_verify.run_id, current.last_verify.status) == ("r1", "failed")
    assert current.last_success is not None
    reloaded = state.record_run(manifest("r2", "ok"), tmp_path)
    assert reloaded.last_verify == current.last_verify
