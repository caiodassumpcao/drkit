import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from drkit import __version__, cli, doctor, runner
from drkit.doctor import Category, Check, Status
from drkit.errors import ConfigError

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
