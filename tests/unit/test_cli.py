import json
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from drkit import __version__, cli, doctor, runner
from drkit.config import LoadedConfig, Manifest
from drkit.doctor import Category, Check, Status
from drkit.errors import ConfigError, DrkitError
from drkit.orchestrator import BackupReport, RetentionResult
from drkit.runmanifest import ProjectState, RunManifest, RunSummary
from drkit.sources.base import ItemResult

cli_runner = CliRunner()


# Rich forces terminal styling on CI (GITHUB_ACTIONS, FORCE_COLOR), which splits words with
# escape sequences.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def invoke(*args: str) -> tuple[int, str, str]:
    result = cli_runner.invoke(cli.app, list(args))
    return result.exit_code, ANSI.sub("", result.stdout), ANSI.sub("", result.stderr)


def test_version() -> None:
    code, out, _ = invoke("--version")
    assert code == 0
    assert out.strip() == f"drkit {__version__}"


def test_help_lists_commands_and_global_options() -> None:
    code, out, _ = invoke("--help")
    assert code == 0
    for text in ("doctor", "--config", "--json", "--verbose", "--version", "DRKIT_CONFIG"):
        assert text in out


def test_no_arguments_shows_help() -> None:
    code, out, err = invoke()
    assert code == 2
    assert "Usage" in out + err


def test_python_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "drkit", "--version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == f"drkit {__version__}"


CHECKS = [
    Check(Category.MANIFEST, "/etc/drkit/dr.yaml", Status.OK, "valid (host h, 1 project)"),
    Check(
        Category.PERMISSIONS,
        "/etc/drkit/restic.pass",
        Status.FAIL,
        "password file is accessible by other users (mode 0644)",
        "chmod 600 /etc/drkit/restic.pass",
    ),
    Check(Category.DEPENDENCIES, "ansible-playbook", Status.WARN, "not found", "install [it]"),
]


@pytest.fixture
def fake_checks(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []

    def run_checks(path: Path) -> list[Check]:
        seen.append(path)
        return CHECKS

    monkeypatch.setattr(doctor, "run_checks", run_checks)
    return seen


def test_doctor_renders_checks_hints_and_exit_code(fake_checks: list[Path]) -> None:
    code, out, _ = invoke("--config", "/tmp/dr.yaml", "doctor")
    assert code == 2
    assert fake_checks == [Path("/tmp/dr.yaml")]
    assert "mode 0644" in out
    assert "How to fix" in out
    assert "chmod 600 /etc/drkit/restic.pass" in out
    assert "install [it]" in out  # rich markup is escaped
    assert "1 check(s) failed." in out


def test_doctor_uses_config_env_var(
    fake_checks: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRKIT_CONFIG", "/srv/dr.yaml")
    invoke("doctor")
    assert fake_checks == [Path("/srv/dr.yaml")]


def test_doctor_all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "run_checks", lambda _: CHECKS[:1])
    code, out, _ = invoke("doctor")
    assert code == 0
    assert "All checks passed." in out


@pytest.mark.usefixtures("fake_checks")
def test_doctor_json_output_is_redacted() -> None:
    runner.redactor.add("restic.pass")
    try:
        code, out, _ = invoke("--json", "doctor")
    finally:
        runner.redactor.clear()
    payload = json.loads(out)
    assert code == 2
    assert payload["ok"] is False
    assert [c["status"] for c in payload["checks"]] == ["ok", "fail", "warn"]
    assert "restic.pass" not in out


def test_drkit_errors_become_messages_and_exit_codes() -> None:
    app = typer.Typer()

    @app.command()
    @cli.handle_errors
    def fail() -> None:
        runner.redactor.add("p4ssw0rd-value")
        raise ConfigError("bad p4ssw0rd-value [x]", hint="fix [it]")

    try:
        result = cli_runner.invoke(app, [])
    finally:
        runner.redactor.clear()
    stderr = ANSI.sub("", result.stderr)
    assert result.exit_code == 2
    assert "error: bad *** [x]" in stderr
    assert "hint: fix [it]" in stderr
    assert "p4ssw0rd-value" not in stderr


def test_main_runs_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["drkit", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0


# --------------------------------------------------------------------------- repository commands

MANIFEST = Manifest.model_validate(
    {
        "version": 1,
        "host": "prod-01",
        "repository": {"url": "s3:https://user:hunter22@s3.example.com/b", "password_file": "/pw"},
        "retention": {"daily": 7},
    }
)
T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)
RUN = RunManifest(
    run_id="20261029T030000Z-a1f3",
    host="prod-01",
    drkit_version="0.1.0",
    started_at=T0,
    finished_at=T0 + timedelta(seconds=12),
    status="partial",
    items=[
        ItemResult(
            kind="db",
            project="shop",
            name="shop",
            engine="postgres",
            engine_version="16.9",
            snapshot="0123456789abcdef",
            bytes=3 * 1024 * 1024,
            duration_s=2.5,
            status="ok",
        ),
        ItemResult(
            kind="db", project="shop", name="legacy", status="failed", error="dump [x] failed"
        ),
    ],
    projects={
        "shop": ProjectState(sha="9c1e2b7a5d3f00112233", dirty=True, images={"app": "app@sha256:1"})
    },
)


class FakeRepo:
    def __init__(self, created: bool = True) -> None:
        self.created = created

    def init(self) -> bool:
        return self.created


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    paths: list[Path] = []

    def load_config(path: Path) -> LoadedConfig:
        paths.append(path)
        return LoadedConfig(
            path=path, manifest=MANIFEST, env={"AWS_SECRET_ACCESS_KEY": "aws-secret-key"}
        )

    monkeypatch.setattr(cli, "load_config", load_config)
    monkeypatch.setattr(cli, "restic_for", lambda manifest, env: FakeRepo())
    yield paths
    runner.redactor.clear()


class TestInit:
    def test_creates_repository(self, loaded: list[Path]) -> None:
        code, out, _ = invoke("-c", "/etc/dr.yaml", "init")
        assert code == 0
        assert loaded == [Path("/etc/dr.yaml")]
        assert "Initialized restic repository" in out
        assert "hunter22" not in out
        assert "outside this server" in out
        # Secrets from the env file are registered for redaction.
        assert runner.redact("aws-secret-key") == runner.REDACTED

    def test_already_initialized(self, loaded: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "restic_for", lambda manifest, env: FakeRepo(created=False))
        code, out, _ = invoke("init")
        assert code == 0
        assert "already initialized" in out

    @pytest.mark.usefixtures("loaded")
    def test_json(self) -> None:
        code, out, _ = invoke("--json", "init")
        assert code == 0
        assert json.loads(out) == {
            "repository": "s3:https://user:***@s3.example.com/b",
            "created": True,
        }


class TestBackup:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch, loaded: list[Path]) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []

        def run_backup(config: LoadedConfig, **kwargs: object) -> BackupReport:
            calls.append(kwargs)
            return BackupReport(
                manifest=RUN,
                retention=RetentionResult(removed_runs=["r0"], forgotten_snapshots=3),
                errors=["state: cannot write"],
                notify_errors=["webhook: HTTP 500"],
            )

        monkeypatch.setattr(cli, "run_backup", run_backup)
        return calls

    def test_renders_run_and_exits_with_partial_failure(
        self, calls: list[dict[str, object]]
    ) -> None:
        code, out, err = invoke("backup", "--project", "shop", "--only", "db, ")
        assert code == 1
        assert calls == [{"project": "shop", "kinds": ["db"]}]
        assert "20261029T030000Z-a1f3" in out
        assert "partial" in out
        assert "postgres 16.9" in out
        assert "3.0 MiB" in out
        assert "01234567" in out
        assert "dump [x] failed" in out
        assert "dropped 1 run(s), 3 snapshot(s)" in out
        assert "warning: state: cannot write" in err
        assert "warning: notification webhook: HTTP 500" in err

    def test_json(self, calls: list[dict[str, object]]) -> None:
        code, out, _ = invoke("--json", "backup")
        payload = json.loads(out)
        assert calls == [{"project": None, "kinds": None}]
        assert payload["run"]["schema"] == 1
        assert payload["run"]["status"] == "partial"
        assert payload["retention"] == {"removed_runs": ["r0"], "forgotten_snapshots": 3}
        assert payload["errors"] == ["state: cannot write"]
        assert code == 1


SUMMARIES = [
    RunSummary("20261028T030000Z-0001", "ok", T0 - timedelta(days=1), "a"),
    RunSummary("20261029T030000Z-a1f3", "partial", T0, "b"),
]


class TestRuns:
    @pytest.fixture(autouse=True)
    def _repo(self, monkeypatch: pytest.MonkeyPatch, loaded: list[Path]) -> None:
        monkeypatch.setattr(cli, "list_runs", lambda restic: SUMMARIES)

    def test_list_newest_first_with_limit(self) -> None:
        code, out, _ = invoke("runs", "list", "-n", "1")
        assert code == 0
        assert "20261029T030000Z-a1f3" in out
        assert "2026-10-29 03:00:00" in out
        assert "20261028" not in out

    def test_list_json(self) -> None:
        _, out, _ = invoke("--json", "runs", "list")
        assert [r["run_id"] for r in json.loads(out)] == [
            "20261029T030000Z-a1f3",
            "20261028T030000Z-0001",
        ]

    def test_list_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "list_runs", lambda restic: [])
        _, out, _ = invoke("runs", "list")
        assert "No runs yet" in out

    def test_show(self, monkeypatch: pytest.MonkeyPatch) -> None:
        requested: list[str] = []

        def load_run(restic: object, run_id: str) -> RunManifest:
            requested.append(run_id)
            return RUN

        monkeypatch.setattr(cli, "load_run", load_run)
        code, out, _ = invoke("runs", "show")
        assert code == 0
        assert requested == ["latest"]
        assert "shop/db/legacy" in out
        assert "shop: git 9c1e2b7a5d3f (dirty), 1 image(s) pinned" in out

        _, out, _ = invoke("--json", "runs", "show", "20261029T030000Z-a1f3")
        assert requested[-1] == "20261029T030000Z-a1f3"
        assert RunManifest.from_json(out) == RUN

    def test_show_unknown_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def load_run(restic: object, run_id: str) -> RunManifest:
            raise DrkitError(f"run {run_id} not found", hint="list the available runs")

        monkeypatch.setattr(cli, "load_run", load_run)
        code, _, err = invoke("runs", "show", "x")
        assert code == 1
        assert "run x not found" in err

    def test_show_run_without_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli, "load_run", lambda restic, run_id: RUN.model_copy(update={"items": []})
        )
        _, out, _ = invoke("runs", "show")
        assert "No items." in out


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1536, "1.5 KiB"),
        (5 * 1024**3, "5.0 GiB"),
        (3 * 1024**4, "3.0 TiB"),
    ],
)
def test_human_bytes(size: int, text: str) -> None:
    assert cli._human_bytes(size) == text
