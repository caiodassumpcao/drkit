"""Every command that changes the state rewrites metrics.textfile, and a metrics file that
cannot be written never changes a command's outcome or exit code."""

import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from drkit import cli, doctor, orchestrator, state, verify
from drkit import drill as drill_module
from drkit.config import LoadedConfig, Manifest, Metrics
from drkit.errors import ExitCode
from drkit.restore import database as database_restore
from drkit.restore import steps as restore_steps
from drkit.restore.steps import RestorePlan, Step
from tests.unit import test_orchestrator as backup_tests
from tests.unit import test_verify as verify_tests
from tests.unit.test_verify import verified  # noqa: F401 - fixture


def with_metrics(manifest: Manifest, textfile: Path) -> Manifest:
    return manifest.model_copy(update={"metrics": Metrics(textfile=textfile)})


@pytest.fixture
def textfile(tmp_path: Path) -> Path:
    (tmp_path / "textfile").mkdir()
    return tmp_path / "textfile" / "drkit.prom"


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    """A metrics path whose directory does not exist: every write fails."""
    return tmp_path / "no-such-directory" / "drkit.prom"


def backup(tmp_path: Path, manifest: Manifest, monkeypatch: pytest.MonkeyPatch) -> Any:
    sources = [backup_tests.FakeSource("shop", "shop")]
    monkeypatch.setattr(orchestrator, "build_sources", lambda *_a, **_k: sources)
    return orchestrator.run_backup(
        LoadedConfig(path=tmp_path / "dr.yaml", manifest=manifest),
        restic=backup_tests.MemoryRestic(),
        lock_path=tmp_path / "drkit.lock",
        state_directory=tmp_path / "state",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


class TestBackup:
    def test_rewrites_the_file(
        self, tmp_path: Path, textfile: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = backup(tmp_path, with_metrics(backup_tests.MANIFEST, textfile), monkeypatch)
        content = textfile.read_text()
        assert 'drkit_last_run_success{host="prod-01"} 1' in content
        assert 'drkit_project_last_success_timestamp_seconds{host="prod-01",project="shop"}' in (
            content
        )
        assert report.exit_code is ExitCode.OK

    def test_unwritable_file_keeps_the_outcome(
        self, tmp_path: Path, broken: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = backup(tmp_path, with_metrics(backup_tests.MANIFEST, broken), monkeypatch)
        assert report.ok
        assert report.errors == []
        assert report.exit_code is ExitCode.OK


class TestVerify:
    @pytest.mark.usefixtures("verified")
    @pytest.mark.parametrize("unwritable", [False, True])
    def test_rewrites_the_file_or_keeps_the_outcome(
        self, tmp_path: Path, textfile: Path, broken: Path, unwritable: bool
    ) -> None:
        target = broken if unwritable else textfile
        report = verify.run_verify(
            LoadedConfig(
                path=tmp_path / "dr.yaml", manifest=with_metrics(verify_tests.MANIFEST, target)
            ),
            restic=verify_tests.FakeRepo(),
            state_directory=tmp_path / "state",
            http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        )
        # The fixture fails one database: the same outcome whether metrics are written or not.
        assert report.exit_code is ExitCode.PARTIAL_FAILURE
        assert report.errors == []
        if not unwritable:
            assert 'drkit_last_verify_success{host="prod-01"} 0' in textfile.read_text()


class TestPrune:
    @pytest.mark.parametrize("unwritable", [False, True])
    def test_rewrites_the_file_or_keeps_the_outcome(
        self, tmp_path: Path, textfile: Path, broken: Path, unwritable: bool
    ) -> None:
        target = broken if unwritable else textfile
        report = orchestrator.run_prune(
            LoadedConfig(
                path=tmp_path / "dr.yaml", manifest=with_metrics(backup_tests.MANIFEST, target)
            ),
            restic=backup_tests.MemoryRestic(),
            lock_path=tmp_path / "drkit.lock",
            state_directory=tmp_path / "state",
        )
        assert report.forgotten_snapshots == 0
        if not unwritable:
            assert 'drkit_last_prune_success{host="prod-01"} 1' in textfile.read_text()


class TestRestoreAndDrill:
    @pytest.fixture
    def cli_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("DRKIT_LOCK_FILE", str(tmp_path / "drkit.lock"))

        def use(textfile: Path) -> None:
            manifest = with_metrics(backup_tests.MANIFEST, textfile)
            monkeypatch.setattr(
                cli, "load_config", lambda path: LoadedConfig(path=path, manifest=manifest)
            )

        class Repo:
            def ensure_ready(self) -> None:
                pass

        monkeypatch.setattr(cli, "restic_for", lambda manifest, env: Repo())
        monkeypatch.setattr(
            database_restore,
            "plan",
            lambda *a, **k: RestorePlan("db", "shop/db/shop", "r1", [Step("x", lambda: "ok")]),
        )
        return use

    @pytest.mark.parametrize("unwritable", [False, True])
    def test_restore(self, cli_env: Any, textfile: Path, broken: Path, unwritable: bool) -> None:
        cli_env(broken if unwritable else textfile)
        result = CliRunner().invoke(cli.app, ["restore", "db", "shop", "--yes"])
        assert result.exit_code == 0, result.output
        if not unwritable:
            assert 'drkit_last_restore_success{host="prod-01",kind="db"} 1' in textfile.read_text()

    @pytest.mark.parametrize("unwritable", [False, True])
    def test_drill(
        self,
        cli_env: Any,
        textfile: Path,
        broken: Path,
        unwritable: bool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli_env(broken if unwritable else textfile)
        config = tmp_path / "dr.yaml"
        config.write_text("operator manifest\n")

        def fake_drill(**kwargs: Any) -> restore_steps.RestoreReport:
            record = state.DrillRecord(
                drill_id="d1", status="ok", finished_at=backup_tests.T0, rto_s=49.9, rpo_s=15.6
            )
            state.record_drill(record)
            plan = RestorePlan("drill", "drkit-drill", "", [Step("x", lambda: "ok")])
            return restore_steps.execute(plan)

        monkeypatch.setattr(drill_module, "run", fake_drill)
        result = CliRunner().invoke(cli.app, ["--config", str(config), "drill", "--local"])
        assert result.exit_code == 0, result.output
        if not unwritable:
            assert 'drkit_last_drill_rto_seconds{host="prod-01"} 49.9' in textfile.read_text()

    def test_drill_without_an_operator_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
        plan = RestorePlan("drill", "drkit-drill", "", [Step("x", lambda: "ok")])
        monkeypatch.setattr(drill_module, "run", lambda **k: restore_steps.execute(plan))
        result = CliRunner().invoke(
            cli.app, ["--config", str(tmp_path / "absent.yaml"), "drill", "--local"]
        )
        assert result.exit_code == 0, result.output


class TestDoctor:
    def test_directory_must_exist_and_be_writable(self, tmp_path: Path, textfile: Path) -> None:
        assert doctor.check_metrics(textfile).status is doctor.Status.OK
        missing = doctor.check_metrics(tmp_path / "absent" / "drkit.prom")
        assert (missing.status, missing.category) == (doctor.Status.FAIL, doctor.Category.METRICS)
        assert missing.detail == f"{tmp_path / 'absent'} not found"
        assert doctor.exit_code([missing]) is ExitCode.PARTIAL_FAILURE

    def test_read_only_directory(self, textfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "access", lambda path, mode: False)
        check = doctor.check_metrics(textfile)
        assert check.detail == f"{textfile.parent} is not writable"
        assert check.hint is not None
