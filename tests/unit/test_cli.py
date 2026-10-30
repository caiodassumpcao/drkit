import json
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from drkit import __version__, cli, doctor, runner
from drkit.config import LoadedConfig, Manifest
from drkit.doctor import Category, Check, Status
from drkit.errors import ConfigError, DrkitError
from drkit.orchestrator import BackupReport, PruneReport, RetentionResult
from drkit.report import StatusReport
from drkit.restore import database as database_restore
from drkit.restore import host as host_restore
from drkit.restore import project as project_restore
from drkit.restore import steps as restore_steps
from drkit.restore.steps import RestorePlan, Step
from drkit.runmanifest import ProjectState, RunManifest, RunSummary
from drkit.sources.base import ItemResult
from drkit.verify import DatabaseCheck, VerifyReport

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

    def ensure_ready(self) -> None:
        pass


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


class TestVerify:
    REPORT = VerifyReport(
        run_id="20261029T030000Z-a1f3",
        repository_error="pack 1a2b is damaged",
        databases=[
            DatabaseCheck("shop/db/shop", "postgres", "postgres:16", "ok", 4, 9.5),
            DatabaseCheck("shop/db/legacy", "mysql", "mysql:8.4", "failed", None, 3.0, "[boom]"),
        ],
        errors=["state: cannot write"],
    )

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch, loaded: list[Path]) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []

        def run_verify(loaded: LoadedConfig, **kwargs: object) -> VerifyReport:
            calls.append(kwargs)
            return self.REPORT

        monkeypatch.setattr(cli, "run_verify", run_verify)
        return calls

    def test_renders_checks(self, calls: list[dict[str, object]]) -> None:
        code, out, err = invoke("verify", "--run", "20261029T030000Z-a1f3")
        assert code == 1
        assert calls == [{"run_id": "20261029T030000Z-a1f3", "read_data_subset": "5%"}]
        assert "repository check: pack 1a2b is damaged" in out
        assert "postgres:16" in out
        assert "[boom]" in out
        assert "warning: state: cannot write" in err

    def test_json_and_structure_only_check(self, calls: list[dict[str, object]]) -> None:
        code, out, _ = invoke("--json", "verify", "--read-data-subset", "0")
        assert calls == [{"run_id": "latest", "read_data_subset": None}]
        payload = json.loads(out)
        assert payload["ok"] is False
        assert [db["status"] for db in payload["databases"]] == ["ok", "failed"]
        assert code == 1

    def test_clean_run_without_databases(
        self, calls: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.REPORT = VerifyReport(run_id="r", repository_error=None)
        code, out, _ = invoke("verify")
        assert code == 0
        assert "✓ repository check" in out
        assert "No database in this run." in out

    @pytest.mark.parametrize("value", ["5%", "2.5%", "1/7", "500M", "2G", "100"])
    def test_accepted_subsets(self, calls: list[dict[str, object]], value: str) -> None:
        invoke("verify", "--read-data-subset", value)
        assert calls[-1]["read_data_subset"] == value

    @pytest.mark.parametrize("value", ["five", "5%%", "-1", "1/"])
    def test_rejected_subsets(self, calls: list[dict[str, object]], value: str) -> None:
        code, _, err = invoke("verify", "--read-data-subset", value)
        assert code == 2
        assert "use a percentage" in err
        assert calls == []


class TestPrune:
    @pytest.fixture(autouse=True)
    def _prune(self, monkeypatch: pytest.MonkeyPatch, loaded: list[Path]) -> None:
        report = PruneReport(orphan_runs=["20261027T030000Z-dead"], forgotten_snapshots=3)
        monkeypatch.setattr(cli, "run_prune", lambda loaded: report)

    def test_reports_forgotten_runs(self) -> None:
        code, out, _ = invoke("prune")
        assert code == 0
        assert "Forgot 3 snapshot(s) of 1 incomplete run(s): 20261027T030000Z-dead" in out
        assert "Repository pruned." in out

    def test_json(self) -> None:
        _, out, _ = invoke("--json", "prune")
        assert json.loads(out) == {
            "orphan_runs": ["20261027T030000Z-dead"],
            "forgotten_snapshots": 3,
        }


class TestReport:
    @pytest.fixture
    def limits(self, monkeypatch: pytest.MonkeyPatch, loaded: list[Path]) -> list[int]:
        limits: list[int] = []
        status = StatusReport(
            host="prod-01",
            generated_at=T0,
            runs=[],
            last_success_run=None,
            last_success_at=None,
            last_verify=None,
        )

        def build_report(restic: object, *, limit: int) -> StatusReport:
            limits.append(limit)
            return status

        monkeypatch.setattr(cli, "build_report", build_report)
        return limits

    def test_markdown(self, limits: list[int]) -> None:
        code, out, _ = invoke("report", "-n", "3")
        assert code == 0
        assert out.startswith("# drkit report: prod-01")
        assert limits == [3]

    @pytest.mark.parametrize("args", [["--json", "report"], ["report", "--format", "json"]])
    def test_json(self, limits: list[int], args: list[str]) -> None:
        _, out, _ = invoke(*args)
        assert json.loads(out)["host"] == "prod-01"
        assert limits == [10]


class TestRestore:
    RUN_ID = "20261029T030000Z-a1f3"

    @staticmethod
    def make_plan(kind: str, *, fail: bool = False) -> RestorePlan:
        def broken() -> str:
            raise DrkitError("pg_restore failed with exit code 1: ERROR: [boom]")

        return RestorePlan(
            kind=kind,  # type: ignore[arg-type]
            target="shop" if kind == "project" else "shop/db/shop",
            run_id=TestRestore.RUN_ID,
            steps=[
                Step("Locate the dumps", lambda: "dump found", ("shop/db/shop: snapshot [abc]",)),
                Step("Restore the dumps", broken if fail else (lambda: None), ("pg_restore",)),
                Step("Sanity check", lambda: "3 table(s)"),
            ],
            summary="Overwrite shop/db/shop with the dump(s) of run r.",
            warnings=["no safety dump"],
            restore_id="r1",
        )

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []
        self.fail = False

        def plan_db(loaded: object, project: str, restic: object, **kwargs: object) -> RestorePlan:
            calls.append({"command": "db", "project": project, **kwargs})
            return self.make_plan("db", fail=self.fail)

        def plan_project(
            loaded: object, project: str, restic: object, **kwargs: object
        ) -> RestorePlan:
            calls.append({"command": "project", "project": project, **kwargs})
            return self.make_plan("project", fail=self.fail)

        def run(plan: RestorePlan, **kwargs: Any) -> restore_steps.RestoreReport:
            calls.append({"command": "run", "from_step": kwargs["from_step"]})
            assert kwargs["host"] == "prod-01"
            assert "notifications" in kwargs
            report = restore_steps.execute(
                plan, from_step=kwargs["from_step"], on_step=kwargs["on_step"]
            )
            report.report_path = tmp_path / "restore-r1.json"
            return report

        monkeypatch.setattr(database_restore, "plan", plan_db)
        monkeypatch.setattr(project_restore, "plan", plan_project)
        monkeypatch.setattr(restore_steps, "run", run)
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        return calls

    @pytest.mark.usefixtures("loaded")
    def test_dry_run_prints_the_plan(self, calls: list[dict[str, object]]) -> None:
        code, out, _ = invoke(
            "restore",
            "db",
            "shop",
            "-d",
            "shop",
            "--into",
            "db2",
            "--no-safety-backup",
            "--run",
            self.RUN_ID,
            "--dry-run",
        )
        assert code == 0
        assert calls == [
            {
                "command": "db",
                "project": "shop",
                "run_id": self.RUN_ID,
                "database": "shop",
                "into": "db2",
                "safety_backup": False,
            }
        ]
        assert f"Restore shop/db/shop from run {self.RUN_ID} (restore r1)" in out
        assert "  1. Locate the dumps" in out
        assert "shop/db/shop: snapshot [abc]" in out
        assert "warning: no safety dump" in out

    @pytest.mark.usefixtures("loaded")
    def test_dry_run_json(self, calls: list[dict[str, object]]) -> None:
        code, out, _ = invoke(
            "--json", "restore", "project", "shop", "--dry-run", "--from-step", "2"
        )
        assert code == 0
        data = json.loads(out)
        assert (data["kind"], data["dry_run"], data["from_step"]) == ("project", True, 2)
        assert [step["number"] for step in data["steps"]] == [1, 2, 3]
        assert calls[0]["to_path"] is None

    @pytest.mark.usefixtures("loaded")
    def test_needs_a_confirmation(self, calls: list[dict[str, object]]) -> None:
        code, _, err = invoke("restore", "db", "shop")
        assert code == 5
        assert "restore needs a confirmation" in err
        assert "--yes" in err
        assert [c["command"] for c in calls] == ["db"]

    @pytest.mark.usefixtures("loaded")
    def test_declined_confirmation(
        self, calls: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_interactive", lambda: True)
        result = cli_runner.invoke(cli.app, ["restore", "db", "shop"], input="n\n")
        assert result.exit_code == 5
        assert "restore cancelled; nothing was changed" in ANSI.sub("", result.stderr)
        assert [c["command"] for c in calls] == ["db"]

    @pytest.mark.usefixtures("loaded")
    def test_confirmed_restore_renders_each_step(
        self, calls: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli, "_interactive", lambda: True)
        result = cli_runner.invoke(
            cli.app,
            ["restore", "project", "shop", "--to-path", "/srv/new", "--no-safety-backup"],
            input="y\n",
        )
        out = ANSI.sub("", result.stdout)
        assert result.exit_code == 0, out
        assert calls[0]["to_path"] == Path("/srv/new")
        assert calls[0]["safety_backup"] is False
        assert "✓ 1. Locate the dumps (0.0s): dump found" in out
        assert "✓ 2. Restore the dumps (0.0s)\n" in out
        assert "Restore completed in" in out
        assert f"Report: {tmp_path / 'restore-r1.json'}" in out

    @pytest.mark.usefixtures("loaded")
    def test_failed_step_exits_with_a_resume_hint(self, calls: list[dict[str, object]]) -> None:
        self.fail = True
        code, out, err = invoke("restore", "db", "shop", "--yes", "--from-step", "1")
        assert code == 5
        assert (
            "✗ 2. Restore the dumps (0.0s): pg_restore failed with exit code 1: ERROR: [boom]"
            in (out)
        )
        assert "- 3. Sanity check: not run" in out
        assert "Restore failed" in out
        assert "restore stopped at step 2 (Restore the dumps)" in err
        assert f"--from-step 2 --run {self.RUN_ID}" in " ".join(err.split())

    @pytest.mark.usefixtures("loaded")
    def test_resume_skips_earlier_steps(self, calls: list[dict[str, object]]) -> None:
        code, out, _ = invoke("restore", "db", "shop", "--yes", "--from-step", "3")
        assert code == 0
        assert "1. Locate the dumps (skipped: --from-step)" in out
        assert "✓ 1." not in out
        assert calls[-1] == {"command": "run", "from_step": 3}

    @pytest.mark.usefixtures("loaded")
    def test_from_step_out_of_range(self, calls: list[dict[str, object]]) -> None:
        code, _, err = invoke("restore", "db", "shop", "--yes", "--from-step", "4")
        assert code == 2
        assert "--from-step must be between 1 and 3" in err

    @pytest.mark.usefixtures("loaded")
    def test_json_report(
        self, calls: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = restore_steps.run

        def run_with_error(plan: RestorePlan, **kwargs: Any) -> restore_steps.RestoreReport:
            report = original(plan, **kwargs)
            report.errors.append("report: cannot write restore report in /var/lib/drkit")
            report.notify_errors.append("webhook: HTTP 500")
            return report

        monkeypatch.setattr(restore_steps, "run", run_with_error)
        code, out, _ = invoke("--json", "restore", "db", "shop", "--yes")
        assert code == 0
        data = json.loads(out)
        assert data["status"] == "ok"
        assert [step["status"] for step in data["steps"]] == ["ok", "ok", "ok"]
        assert data["errors"] == ["report: cannot write restore report in /var/lib/drkit"]

        code, _, err = invoke("restore", "db", "shop", "--yes")
        assert "warning: report: cannot write restore report" in err
        assert "warning: notification webhook: HTTP 500" in err


class TestRestoreHost:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def plan(loaded: object, restic: object, **kwargs: Any) -> RestorePlan:
            calls.append(kwargs)
            projects = restore_steps.StepResult(
                1,
                "Project shop",
                "ok",
                2.0,
                "10 steps",
                substeps=[restore_steps.StepResult(1, "Clone", "ok", 1.0, "cloned")],
            )
            restore_plan = RestorePlan(
                kind="host",
                target="prod-01",
                run_id=TestRestore.RUN_ID,
                steps=[
                    Step("Preflight", lambda: "Ubuntu 24.04"),
                    Step("Restore the projects", lambda: restore_steps.Outcome("shop", [projects])),
                ],
                summary="Rebuild host prod-01.",
                restore_id="r1",
            )
            restore_plan.notes.append("DNS: point shop.example.com to this server")
            restore_plan.facts["rto_s"] = 42.0
            return restore_plan

        def run(plan: RestorePlan, **kwargs: Any) -> restore_steps.RestoreReport:
            return restore_steps.execute(plan, on_step=kwargs["on_step"])

        monkeypatch.setattr(host_restore, "plan", plan)
        monkeypatch.setattr(restore_steps, "run", run)
        return calls

    @pytest.mark.usefixtures("loaded")
    def test_renders_projects_checklist_and_rto(self, calls: list[dict[str, Any]]) -> None:
        code, out, _ = invoke("restore", "host", "--yes", "--skip-system", "--target", "local")
        assert code == 0, out
        assert calls == [{"run_id": "latest", "skip_system": True}]
        assert "✓ 2. Restore the projects" in out
        assert "    ✓ 1. Project shop (2.0s): 10 steps" in out
        assert "        ✓ 1. Clone (1.0s): cloned" in out
        assert "RTO: 42.0s (restore start → all healthchecks green)" in out
        assert "Next steps" in out
        assert "• DNS: point shop.example.com to this server" in out

    @pytest.mark.usefixtures("loaded")
    def test_only_local_target(self, calls: list[dict[str, Any]]) -> None:
        code, _, err = invoke("restore", "host", "--target", "remote", "--dry-run")
        assert code == 2
        assert "remote" in err
