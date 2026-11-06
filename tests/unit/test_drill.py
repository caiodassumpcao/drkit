import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from drkit import cli, doctor, runner, state
from drkit import drill as drill_module
from drkit.config import load_config
from drkit.errors import DrkitError
from drkit.restore import steps
from drkit.restore.steps import Outcome, StepResult
from drkit.runner import CommandResult

T0 = datetime(2026, 11, 6, 3, tzinfo=UTC)


def test_uploads_are_deterministic_and_distinct() -> None:
    contents = [drill_module.upload(i) for i in range(drill_module.UPLOADS)]
    assert contents == [drill_module.upload(i) for i in range(drill_module.UPLOADS)]
    assert len(set(contents)) == drill_module.UPLOADS
    assert all(1024 <= len(c) <= 64 * 1024 and len(c) % 1024 == 0 for c in contents)
    assert drill_module.upload_name(7) == "file-007.bin"


def test_demo_app_is_found_in_the_checkout() -> None:
    demo = drill_module.demo_app_dir()
    assert (demo / "compose.yaml").is_file()
    assert (demo / "app" / "app.py").is_file()
    assert "{domain}" in (demo / "nginx" / "drkit-demo.conf").read_text()


def test_config_is_a_valid_private_manifest(tmp_path: Path) -> None:
    drill = drill_module.Drill(drill_id="d1", work=tmp_path, demo=drill_module.demo_app_dir())
    drill_module.write_config(drill)
    loaded = load_config(drill.config_path)  # also checks owner-only permissions
    manifest = loaded.manifest
    assert manifest.host == "drkit-drill"
    assert manifest.repository.url == "s3:http://127.0.0.1:19000/drkit-drill/d1"
    assert manifest.system.tls is not None
    assert manifest.system.tls.strategy == "restore"
    [project] = manifest.projects
    assert [d.engine for d in project.databases] == ["postgres", "mysql"]
    assert [v.name for v in project.volumes] == ["uploads"]
    assert str(project.healthchecks[0].url) == "http://127.0.0.1:18080/health"
    assert loaded.env["AWS_ACCESS_KEY_ID"] == "drkit-drill"


def test_must_run_as_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    drill = drill_module.Drill(drill_id="d1", work=tmp_path / "w", demo=tmp_path)
    with pytest.raises(DrkitError, match="the drill must run as root"):
        drill_module.prepare(drill)


def test_validate_compares_every_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = {
        "orders": {"rows": 2000, "checksum": "a"},
        "events": {"rows": 2000, "checksum": "b"},
        "uploads": {"file-000.bin": "c"},
    }
    drill = drill_module.Drill(drill_id="d1", work=tmp_path, demo=tmp_path, expected=expected)
    actual = json.loads(json.dumps(expected))
    monkeypatch.setattr(drill_module, "fixtures", lambda drill: actual)
    monkeypatch.setattr(drill_module, "_get", lambda url, **kwargs: None)

    assert drill_module.validate(drill) == (
        "identical: 2000 orders, 2000 events, 1 files; HTTPS through nginx answers"
    )
    assert drill.facts["integrity"] == "identical"

    actual["uploads"]["file-000.bin"] = "tampered"
    actual["events"]["rows"] = 1999
    with pytest.raises(DrkitError, match="differs from the backed-up data: events, uploads"):
        drill_module.validate(drill)
    assert drill.facts["integrity"] == "different"


def test_markdown_summary() -> None:
    report = steps.RestoreReport(
        restore_id="d1", kind="drill", target="drkit-drill", run_id="r1", started_at=T0
    )
    report.finished_at = T0 + timedelta(seconds=120)
    report.steps = [
        StepResult(5, "Back up", "ok", 14.2),
        StepResult(
            7,
            "Restore the host",
            "ok",
            61.0,
            substeps=[
                StepResult(1, "Preflight", "ok", 1.1),
                StepResult(
                    5,
                    "Restore the projects",
                    "ok",
                    41.8,
                    substeps=[
                        StepResult(
                            1,
                            "Project drkit-demo",
                            "ok",
                            41.8,
                            substeps=[StepResult(4, "Pull the images", "ok", 19.6)],
                        )
                    ],
                ),
            ],
        ),
        StepResult(8, "Validate the data", "failed", 0.4, error="restored data differs"),
    ]
    report.facts = {
        "rto_s": 58.3,
        "rpo_s": 4.0,
        "integrity": "different",
        "run_id": "r1",
        "backup_s": 14.2,
        "environment": {
            "runner": "GitHub Actions ubuntu24 20261102",
            "cpus": 4,
            "memory_gib": 15.6,
            "drkit": "1.0.0",
        },
    }

    markdown = drill_module.to_markdown(report)

    assert markdown.startswith("## DR drill FAILED\n")
    assert "| RTO (restore start → all healthchecks green) | **58.3s** |" in markdown
    assert "| RPO (backup end → disaster) | **4s** |" in markdown
    assert "| Environment | GitHub Actions ubuntu24 20261102, 4 CPUs, 15.6 GiB · drkit 1.0.0 |" in (
        markdown
    )
    assert "| 7.1 | Preflight | ok | 1.1s |" in markdown
    assert "| 7.5.1.4 | Pull the images | ok | 19.6s |" in markdown
    assert "**Failed at step 8:** restored data differs" in markdown
    assert drill_module._seconds(None) == "n/a"


class TestRun:
    @pytest.fixture
    def fake_steps(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
        calls: list[str] = []
        monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        def step(name: str, **facts: Any) -> Any:
            def action(drill: drill_module.Drill) -> Any:
                calls.append(name)
                drill.facts.update(facts)
                if name == "restore":
                    return Outcome("RTO 58.3s", [StepResult(1, "Preflight", "ok", 1.0)])
                return name

            return action

        monkeypatch.setattr(drill_module, "prepare", step("prepare"))
        monkeypatch.setattr(drill_module, "start_storage", step("storage"))
        monkeypatch.setattr(drill_module, "deploy", step("deploy"))
        monkeypatch.setattr(drill_module, "seed", step("seed"))
        monkeypatch.setattr(drill_module, "backup", step("backup", run_id="r1", backup_s=12.0))
        monkeypatch.setattr(drill_module, "disaster", step("disaster", rpo_s=3.5))
        monkeypatch.setattr(drill_module, "restore", step("restore", rto_s=58.3))
        monkeypatch.setattr(drill_module, "validate", step("validate", integrity="identical"))
        self.summary = summary
        return calls

    def test_records_the_drill(self, fake_steps: list[str], tmp_path: Path) -> None:
        report = drill_module.run()

        assert report.ok
        assert fake_steps == [
            "prepare", "storage", "deploy", "seed", "backup", "disaster", "restore", "validate",
        ]  # fmt: skip
        assert report.run_id == "r1"
        assert report.steps[6].substeps[0].title == "Preflight"
        assert report.report_path is not None
        assert report.report_path.name == f"drill-{report.restore_id}.json"
        assert json.loads(report.report_path.read_text())["facts"]["rto_s"] == 58.3
        last = state.load(tmp_path / "state").last_drill
        assert last is not None
        assert (last.status, last.rto_s, last.rpo_s) == ("ok", 58.3, 3.5)
        assert self.summary.read_text().startswith("## DR drill passed")

    def test_cli(self, fake_steps: list[str]) -> None:
        result = CliRunner().invoke(cli.app, ["drill", "--local"])
        assert result.exit_code == 0, result.output
        assert "✓ 7. Restore the host (restore host --skip-system)" in result.stdout
        assert "RTO 58.3s · RPO 3.5s · data identical" in result.stdout

        result = CliRunner().invoke(cli.app, ["--json", "drill", "--local"])
        assert json.loads(result.stdout)["facts"]["integrity"] == "identical"


def test_nginx_version_is_read_from_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args: Any, **kwargs: Any) -> CommandResult:
        return CommandResult(tuple(args), 0, "", "nginx version: nginx/1.24.0 (Ubuntu)\n", 0.0)

    monkeypatch.setattr(runner, "run", run)
    check = doctor.check_dependency(drill_module.NGINX)
    assert (check.status, check.detail) == (doctor.Status.OK, "1.24.0")
