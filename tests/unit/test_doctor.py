import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from drkit import docker, doctor, runner
from drkit.doctor import Category, Check, Status
from drkit.errors import CommandTimeoutError, DrkitError, ExitCode, MissingDependencyError
from drkit.runner import CommandResult

HEALTHY: dict[str, str | BaseException] = {
    "restic": "restic 0.18.1 compiled with go1.25.1 on linux/amd64\n",
    "docker version": "29.8.0\n",
    "docker compose": "v2.40.0\n",
    "git": "git version 2.43.0\n",
    "ansible-playbook": "ansible-playbook [core 2.17.4]\n",
    "restic cat": '{"version": 2}',
}

FakeRun = Callable[..., CommandResult]


def fake_run(outputs: Mapping[str, str | BaseException | tuple[int, str]]) -> FakeRun:
    """Answer version commands by their first word(s): text, (exit code, stderr) or exception."""

    def run(args: Sequence[str], **_: object) -> CommandResult:
        key = " ".join(args[:2]) if args[0] == "docker" or args[1:2] == ["cat"] else args[0]
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

    assert (
        doctor.exit_code(
            [check(Category.REPOSITORY, Status.FAIL), check(Category.DEPENDENCIES, Status.FAIL)]
        )
        is ExitCode.MISSING_DEPENDENCY
    )

    assert doctor.exit_code([check(Category.PROJECTS, Status.FAIL)]) is ExitCode.PARTIAL_FAILURE
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


@pytest.mark.usefixtures("healthy")
class TestRepositoryCheck:
    def repo_check(self, manifest: Path) -> Check:
        return by_name(doctor.run_checks(manifest, environ={}))["s3:https://s3.example.com/b"]

    def test_reachable(self, manifest: Path) -> None:
        check = self.repo_check(manifest)
        assert (check.category, check.status) == (Category.REPOSITORY, Status.OK)

    def test_not_initialized(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner, "run", fake_run({**HEALTHY, "restic cat": (10, "no repo")}))
        checks = doctor.run_checks(manifest, environ={})
        check = by_name(checks)["s3:https://s3.example.com/b"]
        assert (check.status, check.detail, check.hint) == (
            Status.FAIL,
            "not initialized",
            "run `drkit init`",
        )
        assert doctor.exit_code(checks) is ExitCode.PARTIAL_FAILURE

    def test_wrong_password(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner, "run", fake_run({**HEALTHY, "restic cat": (12, "wrong")}))
        check = self.repo_check(manifest)
        assert check.status is Status.FAIL
        assert check.detail.startswith("wrong password")

    def test_skipped_without_restic(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        without_restic = {k: v for k, v in HEALTHY.items() if not k.startswith("restic")}
        monkeypatch.setattr(runner, "run", fake_run(without_restic))
        checks = doctor.run_checks(manifest, environ={})
        assert Category.REPOSITORY not in {c.category for c in checks}

    def test_credentials_in_url_are_redacted(self, tmp_path: Path) -> None:
        manifest = write_private(
            tmp_path / "dr.yaml",
            f"""
version: 1
host: prod-01
repository: {{url: "s3:https://key:secret-value@s3.example.com/b", password_file: {tmp_path}/pw}}
retention: {{daily: 7}}
""",
        )
        write_private(tmp_path / "pw", "pw")
        names = [c.name for c in doctor.run_checks(manifest, environ={})]
        assert "s3:https://key:***@s3.example.com/b" in names


@pytest.mark.usefixtures("healthy")
class TestProjectChecks:
    @pytest.fixture
    def with_project(self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        (tmp_path / "shop").mkdir()
        (tmp_path / "shop" / "compose.yaml").write_text("services: {}\n")
        manifest.write_text(
            manifest.read_text()
            + f"""
projects:
  - name: shop
    path: {tmp_path / "shop"}
    git: {{remote: "git@example.com:shop.git", deploy_key: {tmp_path / "key"}}}
    compose: {{project_name: shop-prod, files: [compose.yaml]}}
    databases:
      - {{engine: postgres, service: db, database: shop}}
      - {{engine: postgres, service: db, database: reports}}
      - {{engine: mysql, service: mysql, database: legacy}}
    volumes: [{{name: uploads}}]
"""
        )
        write_private(tmp_path / "key", "deploy key")
        monkeypatch.setattr(
            docker, "compose_config", lambda *args: {"services": {"db": {}, "mysql": {}}}
        )
        monkeypatch.setattr(docker, "compose_container", lambda project, service: "ctr")
        monkeypatch.setattr(docker, "compose_volume", lambda project, volume: f"{project}_{volume}")
        return manifest

    def projects(self, manifest: Path) -> dict[str, Check]:
        checks = doctor.run_checks(manifest, environ={})
        return {c.name: c for c in checks if c.category is Category.PROJECTS}

    def test_all_ok(self, with_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        remotes: list[list[str]] = []

        def run(args: Sequence[str], **kwargs: object) -> CommandResult:
            if list(args[3:5]) == ["ls-remote", "--heads"]:
                remotes.append(list(args))
            return fake_run(HEALTHY)(args, **kwargs)

        monkeypatch.setattr(runner, "run", run)
        checks = doctor.run_checks(with_project, environ={})

        projects = {c.name: c for c in checks if c.category is Category.PROJECTS}
        assert {name: (c.status, c.detail) for name, c in projects.items()} == {
            "shop: directory": (Status.OK, str(with_project.parent / "shop")),
            "shop: compose files": (Status.OK, "valid (2 services)"),
            "shop: git remote": (Status.OK, "readable with the deploy key"),
            "shop: service db": (Status.OK, "running"),
            "shop: service mysql": (Status.OK, "running"),
            "shop: volume uploads": (Status.OK, "shop-prod_uploads"),
        }
        assert doctor.exit_code(checks) is ExitCode.OK
        [ls_remote] = remotes
        assert ls_remote[-1] == "git@example.com:shop.git"
        assert f"-i {with_project.parent / 'key'}" in ls_remote[2]

    def test_service_not_running(self, with_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []

        def compose_container(project: str, service: str) -> str:
            calls.append((project, service))
            if service == "mysql":
                raise docker.ServiceNotRunningError("service 'mysql' has no running container")
            return "ctr"

        monkeypatch.setattr(docker, "compose_container", compose_container)
        checks = doctor.run_checks(with_project, environ={})
        projects = by_name(checks)
        assert projects["shop: service mysql"].status is Status.FAIL
        assert projects["shop: service mysql"].detail == (
            "service 'mysql' has no running container"
        )
        assert calls == [("shop-prod", "db"), ("shop-prod", "mysql")]
        assert doctor.exit_code(checks) is ExitCode.PARTIAL_FAILURE

    def test_missing_directory_and_volume(
        self, with_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shutil.rmtree(with_project.parent / "shop")

        def compose_volume(project: str, volume: str) -> str:
            raise DrkitError("volume 'uploads' not found", hint="docker compose up")

        monkeypatch.setattr(docker, "compose_volume", compose_volume)
        monkeypatch.setattr(docker, "compose_config", lambda *args: {})
        projects = self.projects(with_project)
        assert projects["shop: directory"].status is Status.FAIL
        assert "shop: compose files" not in projects
        assert projects["shop: volume uploads"].hint == "docker compose up"

    def test_missing_compose_file(self, with_project: Path) -> None:
        (with_project.parent / "shop" / "compose.yaml").unlink()
        check = self.projects(with_project)["shop: compose files"]
        assert check.status is Status.FAIL
        assert check.detail == f"not found: {with_project.parent / 'shop' / 'compose.yaml'}"

    def test_invalid_compose_files(
        self, with_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def compose_config(*args: object) -> object:
            raise DrkitError("invalid compose configuration in /srv/shop: bad", hint="fix it")

        monkeypatch.setattr(docker, "compose_config", compose_config)
        check = self.projects(with_project)["shop: compose files"]
        assert (check.status, check.hint) == (Status.FAIL, "fix it")

    def test_unreadable_git_remote(
        self, with_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outputs = {**HEALTHY, "git": (128, "Permission denied (publickey).")}

        def run(args: Sequence[str], **kwargs: object) -> CommandResult:
            if "ls-remote" in args:
                return fake_run(outputs)(args, **kwargs)
            return fake_run(HEALTHY)(args, **kwargs)

        monkeypatch.setattr(runner, "run", run)
        check = self.projects(with_project)["shop: git remote"]
        assert check.status is Status.FAIL
        assert check.detail == (
            "cannot read git remote git@example.com:shop.git: Permission denied (publickey)."
        )
        assert check.hint is not None
        assert "is a deploy key with read access" in check.hint

    def test_external_volume(self, with_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def compose_volume(project: str, volume: str) -> str:
            raise DrkitError("volume 'uploads' not found")

        monkeypatch.setattr(docker, "compose_volume", compose_volume)
        monkeypatch.setattr(
            docker, "compose_config", lambda *args: {"volumes": {"uploads": {"external": True}}}
        )
        check = self.projects(with_project)["shop: volume uploads"]
        assert check.status is Status.FAIL
        assert "declared external" in check.detail

    def test_git_check_needs_git(self, with_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        without_git = {k: v for k, v in HEALTHY.items() if k != "git"}
        monkeypatch.setattr(runner, "run", fake_run(without_git))
        assert "shop: git remote" not in self.projects(with_project)

    def test_skipped_without_docker(
        self, with_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        without_docker = {k: v for k, v in HEALTHY.items() if k != "docker version"}
        monkeypatch.setattr(runner, "run", fake_run(without_docker))
        checks = doctor.run_checks(with_project, environ={})
        assert not [c for c in checks if c.category is Category.PROJECTS]
