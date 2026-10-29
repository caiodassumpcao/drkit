from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from drkit import doctor, runner
from drkit.doctor import Category, Check, Status
from drkit.errors import CommandTimeoutError, ExitCode, MissingDependencyError
from drkit.runner import CommandResult

HEALTHY: dict[str, str | BaseException] = {
    "restic": "restic 0.18.1 compiled with go1.25.1 on linux/amd64\n",
    "docker version": "29.8.0\n",
    "docker compose": "v2.40.0\n",
    "git": "git version 2.43.0\n",
    "ansible-playbook": "ansible-playbook [core 2.17.4]\n",
}

FakeRun = Callable[..., CommandResult]


def fake_run(outputs: Mapping[str, str | BaseException | tuple[int, str]]) -> FakeRun:
    """Answer version commands by their first word(s): text, (exit code, stderr) or exception."""

    def run(args: Sequence[str], **_: object) -> CommandResult:
        key = " ".join(args[:2]) if args[0] == "docker" else args[0]
        output = outputs.get(key, MissingDependencyError(f"command not found: {args[0]}"))
        if isinstance(output, BaseException):
            raise output
        code, stdout, stderr = (
            (output[0], "", output[1]) if isinstance(output, tuple) else (0, output, "")
        )
        return CommandResult(tuple(args), code, stdout, stderr, 0.01)

    return run


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "run", fake_run(HEALTHY))


def write_private(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o600)
    return path


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    password = write_private(tmp_path / "restic.pass", "pw")
    return write_private(
        tmp_path / "dr.yaml",
        f"""
version: 1
host: prod-01
repository: {{url: "s3:https://s3.example.com/b", password_file: {password}}}
retention: {{daily: 7}}
""",
    )


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


@pytest.mark.usefixtures("healthy")
class TestRunChecks:
    def test_all_green(self, manifest: Path) -> None:
        checks = doctor.run_checks(manifest, environ={})
        assert [c.status for c in checks] == [Status.OK] * len(checks)
        assert checks[0].detail == "valid (host prod-01, 0 projects)"
        assert [c.name for c in checks if c.category is Category.DEPENDENCIES] == [
            "restic",
            "docker",
            "docker compose",
            "git",
        ]
        assert doctor.exit_code(checks) is ExitCode.OK

    def test_ansible_is_required_when_configured(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest.write_text(manifest.read_text() + "system: {ansible: {source: /opt/iac}}\n")
        monkeypatch.setattr(runner, "run", fake_run({**HEALTHY, "ansible-playbook": (1, "boom")}))
        checks = doctor.run_checks(manifest, environ={})
        assert by_name(checks)["ansible-playbook"].status is Status.FAIL
        assert doctor.exit_code(checks) is ExitCode.MISSING_DEPENDENCY

    def test_invalid_manifest_still_checks_everything_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        without_ansible = {k: v for k, v in HEALTHY.items() if k != "ansible-playbook"}
        monkeypatch.setattr(runner, "run", fake_run(without_ansible))
        manifest = write_private(tmp_path / "dr.yaml", "version: 1\nhost: X\n")
        checks = doctor.run_checks(manifest, environ={})
        first = checks[0]
        assert first.category is Category.MANIFEST
        assert first.status is Status.FAIL
        assert "host: must be 2-41" in first.detail
        assert by_name(checks)["restic"].status is Status.OK
        # Unknown whether Ansible is needed: reported, but only as a warning.
        assert by_name(checks)["ansible-playbook"].status is Status.WARN
        assert doctor.exit_code(checks) is ExitCode.CONFIG_ERROR

    def test_insecure_files_are_reported_with_labels(self, manifest: Path, tmp_path: Path) -> None:
        manifest.chmod(0o644)
        (tmp_path / "restic.pass").chmod(0o604)
        checks = [
            c for c in doctor.run_checks(manifest, environ={}) if c.category is Category.PERMISSIONS
        ]
        assert [(c.status, c.detail.split(" ")[0]) for c in checks] == [
            (Status.FAIL, "manifest"),
            (Status.FAIL, "password"),
        ]
        assert checks[1].hint == f"chmod 600 {tmp_path / 'restic.pass'}"
        assert doctor.exit_code(checks) is ExitCode.CONFIG_ERROR

    def test_missing_manifest(self, tmp_path: Path) -> None:
        checks = doctor.run_checks(tmp_path / "missing.yaml", environ={})
        assert checks[0].detail.startswith("manifest not found")
        assert checks[1].category is Category.PERMISSIONS
        assert checks[1].status is Status.FAIL

    def test_manifest_secrets_are_registered_for_redaction(self, manifest: Path) -> None:
        manifest.write_text(
            manifest.read_text()
            + "notifications: {webhook: {url: 'https://hooks.example.com/T0/very-secret'}}\n"
        )
        runner.redactor.clear()
        doctor.run_checks(manifest, environ={})
        assert runner.redact("https://hooks.example.com/T0/very-secret") == runner.REDACTED
        runner.redactor.clear()


class TestCheckDependency:
    def run_with(
        self, monkeypatch: pytest.MonkeyPatch, output: str | BaseException | tuple[int, str]
    ) -> Check:
        monkeypatch.setattr(runner, "run", fake_run({"restic": output, "docker version": output}))
        return doctor.check_dependency(doctor.RESTIC)

    def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = self.run_with(monkeypatch, "restic 0.17.0 compiled with go")
        assert (check.status, check.detail) == (Status.OK, "0.17.0")

    def test_too_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = self.run_with(monkeypatch, "restic 0.16.4 compiled with go")
        assert check.status is Status.FAIL
        assert check.detail == "0.16.4 is older than 0.17.0"
        assert check.hint is not None
        assert "restic/releases" in check.hint

    def test_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner, "run", fake_run({}))
        check = doctor.check_dependency(doctor.RESTIC)
        assert (check.status, check.detail) == (Status.FAIL, "not found in PATH")

    def test_optional_dependency_only_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner, "run", fake_run({}))
        assert doctor.check_dependency(doctor.ANSIBLE, required=False).status is Status.WARN

    def test_unparseable_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = self.run_with(monkeypatch, "something else")
        assert check.detail == "could not determine version"

    def test_command_failure_shows_last_stderr_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = self.run_with(monkeypatch, (1, "warning\nfatal: broken"))
        assert check.detail == "fatal: broken"

    def test_command_failure_without_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = self.run_with(monkeypatch, (3, ""))
        assert check.detail == "exit code 3"

    def test_docker_daemon_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            runner,
            "run",
            fake_run({"docker version": (1, "permission denied while trying to connect")}),
        )
        check = doctor.check_dependency(doctor.DOCKER)
        assert check.status is Status.FAIL
        assert check.hint is not None
        assert "Docker daemon" in check.hint

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            runner, "run", fake_run({"docker version": CommandTimeoutError("slow")})
        )
        check = doctor.check_dependency(doctor.DOCKER)
        assert check.detail == "no answer within 15s"


def test_exit_code_precedence() -> None:
    def check(category: Category, status: Status) -> Check:
        return Check(category, "x", status, "")

    assert doctor.exit_code([]) is ExitCode.OK
    assert doctor.exit_code([check(Category.DEPENDENCIES, Status.WARN)]) is ExitCode.OK
    assert (
        doctor.exit_code(
            [check(Category.DEPENDENCIES, Status.FAIL), check(Category.PERMISSIONS, Status.FAIL)]
        )
        is ExitCode.CONFIG_ERROR
    )


def test_parse_version() -> None:
    assert doctor.parse_version("2.15.0") > doctor.parse_version("2.9.12")


def test_check_to_dict() -> None:
    check = Check(Category.MANIFEST, "dr.yaml", Status.OK, "valid")
    assert check.to_dict() == {
        "category": "manifest",
        "name": "dr.yaml",
        "status": "ok",
        "detail": "valid",
        "hint": None,
    }
