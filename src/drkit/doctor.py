"""Environment checks behind ``drkit doctor``.

Every check runs and is reported, so a single invocation shows everything that needs fixing.
Rendering lives in the CLI; this module only produces results.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from drkit import docker, runner
from drkit.config import LoadedConfig, Project, check_private_file, load_config
from drkit.errors import (
    CommandTimeoutError,
    ConfigError,
    DrkitError,
    ExitCode,
    MissingDependencyError,
)
from drkit.restic import Restic

COMMAND_TIMEOUT_S = 15.0


class Category(StrEnum):
    MANIFEST = "manifest"
    PERMISSIONS = "permissions"
    DEPENDENCIES = "dependencies"
    REPOSITORY = "repository"
    PROJECTS = "projects"


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    category: Category
    name: str
    status: Status
    detail: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    version_command: tuple[str, ...]
    version_pattern: re.Pattern[str]
    minimum: tuple[int, ...] | None
    install_hint: str


RESTIC = Dependency(
    name="restic",
    version_command=("restic", "version"),
    version_pattern=re.compile(r"restic (\d+\.\d+\.\d+)"),
    minimum=(0, 17, 0),
    install_hint="install restic >= 0.17 from https://github.com/restic/restic/releases "
    "(older distributions ship outdated packages)",
)
DOCKER = Dependency(
    name="docker",
    version_command=("docker", "version", "--format", "{{.Server.Version}}"),
    version_pattern=re.compile(r"(\d+\.\d+\.\d+)"),
    minimum=(24, 0, 0),
    install_hint="install Docker Engine: https://docs.docker.com/engine/install/ubuntu/",
)
DOCKER_COMPOSE = Dependency(
    name="docker compose",
    version_command=("docker", "compose", "version", "--short"),
    version_pattern=re.compile(r"v?(\d+\.\d+\.\d+)"),
    minimum=(2, 0, 0),
    install_hint="install the Compose v2 plugin: apt-get install docker-compose-plugin",
)
GIT = Dependency(
    name="git",
    version_command=("git", "--version"),
    version_pattern=re.compile(r"git version (\d+\.\d+\.\d+)"),
    minimum=None,
    install_hint="apt-get install git",
)
ANSIBLE = Dependency(
    name="ansible-playbook",
    version_command=("ansible-playbook", "--version"),
    version_pattern=re.compile(r"core (\d+\.\d+\.\d+)"),
    minimum=(2, 15, 0),
    install_hint="install ansible-core >= 2.15 (e.g. pipx install ansible-core)",
)
REQUIRED_DEPENDENCIES = (RESTIC, DOCKER, DOCKER_COMPOSE, GIT)


def run_checks(config_path: Path, *, environ: Mapping[str, str] | None = None) -> list[Check]:
    """Run every check against the manifest at ``config_path``."""
    checks: list[Check] = []

    loaded: LoadedConfig | None = None
    try:
        loaded = load_config(config_path, environ=environ, check_permissions=False)
    except ConfigError as exc:
        checks.append(
            Check(Category.MANIFEST, str(config_path), Status.FAIL, exc.message, exc.hint)
        )
    else:
        runner.redactor.add_all(loaded.secret_values())
        manifest = loaded.manifest
        checks.append(
            Check(
                Category.MANIFEST,
                str(config_path),
                Status.OK,
                f"valid (host {manifest.host}, {_plural(len(manifest.projects), 'project')})",
            )
        )

    private_files = {config_path: "manifest"}
    if loaded is not None:
        private_files.update(loaded.manifest.private_files())
    checks.extend(check_permissions(private_files))

    if loaded is None:
        # Without a manifest we cannot know whether Ansible is needed: report it, never fail.
        optional: Sequence[Dependency] = (ANSIBLE,)
        required: Sequence[Dependency] = REQUIRED_DEPENDENCIES
    elif loaded.manifest.system.ansible is not None:
        optional, required = (), (*REQUIRED_DEPENDENCIES, ANSIBLE)
    else:
        optional, required = (), REQUIRED_DEPENDENCIES
    dependency_checks = [check_dependency(dep) for dep in required]
    dependency_checks += [check_dependency(dep, required=False) for dep in optional]
    checks.extend(dependency_checks)

    def available(dependency: Dependency) -> bool:
        return any(c.name == dependency.name and c.status is Status.OK for c in dependency_checks)

    if loaded is not None and available(RESTIC):
        manifest = loaded.manifest
        checks.append(
            check_repository(Restic(manifest.repository, host=manifest.host, env=loaded.env))
        )
    if loaded is not None and available(DOCKER):
        for project in loaded.manifest.projects:
            checks.extend(check_project(project))
    return checks


def check_project(project: Project) -> list[Check]:
    """The project directory exists, its database services run and its volumes exist."""

    def result(name: str, status: Status, detail: str, hint: str | None = None) -> Check:
        return Check(Category.PROJECTS, f"{project.name}: {name}", status, detail, hint)

    checks = [
        result("directory", Status.OK, str(project.path))
        if project.path.is_dir()
        else result(
            "directory",
            Status.FAIL,
            f"{project.path} not found",
            "clone the project there, or fix projects[].path",
        )
    ]
    compose_project = project.compose_project
    for service in dict.fromkeys(db.service for db in project.databases):
        try:
            docker.compose_container(compose_project, service)
        except DrkitError as exc:
            checks.append(result(f"service {service}", Status.FAIL, exc.message, exc.hint))
        else:
            checks.append(result(f"service {service}", Status.OK, "running"))
    for volume in project.volumes:
        try:
            name = docker.compose_volume(compose_project, volume.name)
        except DrkitError as exc:
            checks.append(result(f"volume {volume.name}", Status.FAIL, exc.message, exc.hint))
        else:
            checks.append(result(f"volume {volume.name}", Status.OK, name))
    return checks


def check_repository(restic: Restic) -> Check:
    """Open the repository: proves network access, credentials and the password at once."""
    name = runner.redact(restic.url)
    try:
        initialized = restic.exists()
    except DrkitError as exc:
        return Check(Category.REPOSITORY, name, Status.FAIL, exc.message, exc.hint)
    if not initialized:
        return Check(Category.REPOSITORY, name, Status.FAIL, "not initialized", "run `drkit init`")
    return Check(Category.REPOSITORY, name, Status.OK, "reachable, password accepted")


def check_permissions(files: Mapping[Path, str]) -> list[Check]:
    """Check each ``path: label`` pair holds secrets safely (owner-only access)."""
    checks = []
    for path, label in files.items():
        try:
            check_private_file(path, label=label)
        except ConfigError as exc:
            checks.append(
                Check(Category.PERMISSIONS, str(path), Status.FAIL, exc.message, exc.hint)
            )
        else:
            checks.append(
                Check(Category.PERMISSIONS, str(path), Status.OK, f"{label}: owner-only access")
            )
    return checks


def check_dependency(dependency: Dependency, *, required: bool = True) -> Check:
    failure = Status.FAIL if required else Status.WARN

    def result(status: Status, detail: str, hint: str | None = None) -> Check:
        return Check(Category.DEPENDENCIES, dependency.name, status, detail, hint)

    try:
        output = runner.run(dependency.version_command, timeout=COMMAND_TIMEOUT_S, check=False)
    except MissingDependencyError:
        return result(failure, "not found in PATH", dependency.install_hint)
    except CommandTimeoutError:
        return result(failure, f"no answer within {COMMAND_TIMEOUT_S:g}s", _daemon_hint(dependency))

    if not output.ok:
        detail = runner.failure_detail(output, max_lines=1) or f"exit code {output.returncode}"
        return result(failure, detail, _daemon_hint(dependency) or dependency.install_hint)

    match = dependency.version_pattern.search(output.stdout)
    if match is None:
        return result(failure, "could not determine version", dependency.install_hint)
    version = match.group(1)
    if dependency.minimum is not None and parse_version(version) < dependency.minimum:
        minimum = ".".join(map(str, dependency.minimum))
        return result(failure, f"{version} is older than {minimum}", dependency.install_hint)
    return result(Status.OK, version)


def exit_code(checks: Iterable[Check]) -> ExitCode:
    """Configuration problems first, then missing dependencies, then the rest (repository
    access, project services)."""
    failed = {c.category for c in checks if c.status is Status.FAIL}
    if failed & {Category.MANIFEST, Category.PERMISSIONS}:
        return ExitCode.CONFIG_ERROR
    if Category.DEPENDENCIES in failed:
        return ExitCode.MISSING_DEPENDENCY
    if failed:
        return ExitCode.PARTIAL_FAILURE
    return ExitCode.OK


def parse_version(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _daemon_hint(dependency: Dependency) -> str | None:
    if dependency is DOCKER:
        return "start the Docker daemon and run drkit as root (it needs the Docker socket)"
    return None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"
