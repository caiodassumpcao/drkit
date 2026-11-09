import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from drkit import __version__, cli, metrics, state
from drkit.config import LoadedConfig, Manifest, Metrics

T0 = datetime(2026, 11, 9, 3, tzinfo=UTC)


def full_state() -> state.State:
    run = state.RunRecord(
        run_id="r2",
        status="partial",
        started_at=T0,
        finished_at=T0 + timedelta(seconds=42.5),
        items_ok=5,
        items_failed=1,
        bytes_added=1048576,
    )
    return state.State(
        last_run=run,
        last_success=run.model_copy(update={"run_id": "r1", "status": "ok"}),
        last_verify=state.VerifyRecord(run_id="r1", status="ok", finished_at=T0),
        last_drill=state.DrillRecord(
            drill_id="d1", status="ok", finished_at=T0, rto_s=49.9, rpo_s=15.6
        ),
        projects={
            "shop": state.ProjectRecord(run_id="r2", last_success_at=T0),
            "blog": state.ProjectRecord(run_id="r1", last_success_at=T0 - timedelta(hours=1)),
        },
        last_prune=state.PruneRecord(status="failed", finished_at=T0),
        last_restores={
            "host": state.RestoreRecord(restore_id="h1", status="ok", finished_at=T0, rto_s=268.4),
            "db": state.RestoreRecord(restore_id="x1", status="failed", finished_at=T0),
        },
    )


def manifest(textfile: Path | None = None) -> Manifest:
    data: dict[str, object] = {
        "version": 1,
        "host": "prod-01",
        "repository": {"url": "s3:https://s3.example.com/b", "password_file": "/pw"},
        "retention": {"daily": 7},
    }
    if textfile is not None:
        data["metrics"] = {"textfile": str(textfile)}
    return Manifest.model_validate(data)


def samples(text: str) -> dict[str, str]:
    return dict(
        line.rsplit(" ", 1) for line in text.splitlines() if line and not line.startswith("#")
    )


def test_every_metric_of_a_full_state() -> None:
    text = metrics.render(metrics.collect(full_state(), "prod-01"))
    ts = str(int(T0.timestamp()))
    assert samples(text) == {
        f'drkit_info{{host="prod-01",version="{__version__}"}}': "1",
        'drkit_last_run_timestamp_seconds{host="prod-01"}': repr(T0.timestamp() + 42.5),
        'drkit_last_success_timestamp_seconds{host="prod-01"}': repr(T0.timestamp() + 42.5),
        'drkit_last_run_duration_seconds{host="prod-01"}': "42.5",
        'drkit_last_run_success{host="prod-01"}': "0",
        'drkit_last_run_items{host="prod-01",status="ok"}': "5",
        'drkit_last_run_items{host="prod-01",status="failed"}': "1",
        'drkit_last_run_bytes_added{host="prod-01"}': "1048576",
        'drkit_project_last_success_timestamp_seconds{host="prod-01",project="blog"}': str(
            int((T0 - timedelta(hours=1)).timestamp())
        ),
        'drkit_project_last_success_timestamp_seconds{host="prod-01",project="shop"}': ts,
        'drkit_last_verify_timestamp_seconds{host="prod-01"}': ts,
        'drkit_last_verify_success{host="prod-01"}': "1",
        'drkit_last_prune_timestamp_seconds{host="prod-01"}': ts,
        'drkit_last_prune_success{host="prod-01"}': "0",
        'drkit_last_restore_timestamp_seconds{host="prod-01",kind="db"}': ts,
        'drkit_last_restore_timestamp_seconds{host="prod-01",kind="host"}': ts,
        'drkit_last_restore_success{host="prod-01",kind="db"}': "0",
        'drkit_last_restore_success{host="prod-01",kind="host"}': "1",
        'drkit_last_restore_rto_seconds{host="prod-01",kind="host"}': "268.4",
        'drkit_last_drill_timestamp_seconds{host="prod-01"}': ts,
        'drkit_last_drill_success{host="prod-01"}': "1",
        'drkit_last_drill_rto_seconds{host="prod-01"}': "49.9",
        'drkit_last_drill_rpo_seconds{host="prod-01"}': "15.6",
    }


def test_help_and_type_precede_each_family() -> None:
    lines = metrics.render(metrics.collect(full_state(), "prod-01")).splitlines()
    names = [line.split()[2] for line in lines if line.startswith("# TYPE")]
    assert len(names) == len(set(names)) == 19
    for name in names:
        index = lines.index(f"# TYPE {name} gauge")
        assert lines[index - 1].startswith(f"# HELP {name} ")
        assert lines[index + 1].startswith(f"{name}{{")


def test_metrics_without_data_are_left_out() -> None:
    text = metrics.render(metrics.collect(state.State(), "prod-01"))
    assert text == (
        "# HELP drkit_info drkit version, always 1.\n"
        "# TYPE drkit_info gauge\n"
        f'drkit_info{{host="prod-01",version="{__version__}"}} 1\n'
    )
    # A run recorded by 1.0 has no item counts nor bytes: those stay out, never as 0.
    old_run = state.RunRecord(run_id="r", status="ok", started_at=T0, finished_at=T0)
    text = metrics.render(metrics.collect(state.State(last_run=old_run), "h"))
    assert "drkit_last_run_items" not in text
    assert "drkit_last_run_bytes_added" not in text
    assert "drkit_last_success_timestamp_seconds" not in text
    assert 'drkit_last_run_success{host="h"} 1' in text


def test_failed_restores_have_no_rto() -> None:
    text = metrics.render(metrics.collect(full_state(), "prod-01"))
    assert 'drkit_last_restore_rto_seconds{host="prod-01",kind="db"}' not in text


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("plain", "plain"),
        ('say "hi"', 'say \\"hi\\"'),
        ("back\\slash", "back\\\\slash"),
        ("two\nlines", "two\\nlines"),
    ],
)
def test_label_values_are_escaped(value: str, escaped: str) -> None:
    assert metrics.escape_label(value) == escaped
    text = metrics.render([metrics.Family("x", "help", [({"project": value}, 1)])])
    assert f'x{{project="{escaped}"}} 1' in text


def test_values() -> None:
    assert metrics.format_value(1) == "1"
    assert metrics.format_value(3.0) == "3"
    assert metrics.format_value(0.25) == "0.25"


class TestWrite:
    def test_atomic_world_readable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        real_replace = Path.replace

        def replace(self: Path, target: Path) -> Path:
            seen.append(self.name)
            assert self.parent == target.parent, "the rename must stay in one directory"
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", replace)
        target = tmp_path / "drkit.prom"
        target.write_text("old\n")

        metrics.write(target, "new\n")

        assert target.read_text() == "new\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        [temporary] = seen
        assert temporary.startswith(".drkit.prom.") and not temporary.endswith(".prom")
        assert [p.name for p in tmp_path.iterdir()] == ["drkit.prom"]

    def test_failure_leaves_the_old_file_and_no_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "drkit.prom"
        target.write_text("old\n")

        def broken_fsync(fd: int) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", broken_fsync)
        with pytest.raises(OSError, match="No space left"):
            metrics.write(target, "new\n")
        assert target.read_text() == "old\n"
        assert [p.name for p in tmp_path.iterdir()] == ["drkit.prom"]


class TestRefresh:
    def test_writes_the_configured_file(self, tmp_path: Path) -> None:
        state.save(full_state(), tmp_path / "state")
        target = tmp_path / "drkit.prom"
        assert metrics.refresh(manifest(target), tmp_path / "state") is None
        assert 'drkit_last_drill_rpo_seconds{host="prod-01"} 15.6' in target.read_text()

    def test_not_configured(self, tmp_path: Path) -> None:
        assert metrics.refresh(manifest(), tmp_path) is None
        assert list(tmp_path.iterdir()) == []

    def test_failure_is_reported_never_raised(self, tmp_path: Path) -> None:
        error = metrics.refresh(manifest(tmp_path / "missing" / "drkit.prom"), tmp_path)
        assert error is not None
        assert error.startswith(f"cannot write metrics to {tmp_path / 'missing' / 'drkit.prom'}:")
        (tmp_path / "state.json").write_text("{broken")
        assert metrics.refresh(manifest(tmp_path / "drkit.prom"), tmp_path) is not None


def test_textfile_must_be_a_prom_file() -> None:
    assert Metrics(textfile=Path("/var/lib/node_exporter/drkit.prom")).textfile.name == "drkit.prom"
    with pytest.raises(ValidationError, match=r"must end in \.prom"):
        Metrics(textfile=Path("/var/lib/node_exporter/drkit.txt"))
    with pytest.raises(ValidationError, match="must be an absolute path"):
        Metrics(textfile=Path("drkit.prom"))


class TestCommand:
    @pytest.fixture
    def loaded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
        state.save(full_state(), tmp_path / "state")
        monkeypatch.setattr(
            cli, "load_config", lambda path: LoadedConfig(path=path, manifest=manifest())
        )
        return tmp_path

    def test_stdout(self, loaded: Path) -> None:
        result = CliRunner().invoke(cli.app, ["metrics"])
        assert result.exit_code == 0, result.output
        assert result.stdout.startswith("# HELP drkit_info ")
        assert 'drkit_last_run_items{host="prod-01",status="failed"} 1' in result.stdout

    def test_output_file(self, loaded: Path) -> None:
        target = loaded / "drkit.prom"
        result = CliRunner().invoke(cli.app, ["metrics", "--output", str(target)])
        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert target.read_text().startswith("# HELP drkit_info ")

    def test_unwritable_output(self, loaded: Path) -> None:
        result = CliRunner().invoke(
            cli.app, ["metrics", "--output", str(loaded / "missing" / "drkit.prom")]
        )
        assert result.exit_code == 2
        assert "cannot write metrics to" in result.stderr
