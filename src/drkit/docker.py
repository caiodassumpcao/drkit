"""Docker and Compose access through the CLI (ADR-0003).

Containers are found by the labels Compose sets on them, so drkit needs neither the compose
files nor the project directory to locate a service.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from drkit import runner
from drkit.errors import DrkitError

COMMAND_TIMEOUT_S = 60.0


log = structlog.get_logger(__name__)


class ServiceNotRunningError(DrkitError):
    """A compose service has no running container."""


def compose_container(project: str, service: str) -> str:
    """ID of a running container of ``service`` in compose ``project``.

    With several replicas, the first by name is used: any replica can serve a dump.
    """
    result = runner.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--filter",
            "status=running",
            "--format",
            "{{.Names}}\t{{.ID}}",
        ],
        timeout=COMMAND_TIMEOUT_S,
    )
    containers = sorted(line.split("\t") for line in result.stdout.splitlines() if "\t" in line)
    if not containers:
        raise ServiceNotRunningError(
            f"service {service!r} of compose project {project!r} has no running container",
            hint=f"start it with `docker compose -p {project} up -d {service}`, or fix "
            "compose.project_name / databases[].service in the manifest",
        )
    if len(containers) > 1:
        log.debug("docker.replicas", project=project, service=service, count=len(containers))
    name, container_id = containers[0]
    log.debug("docker.container", project=project, service=service, container=name)
    return container_id


def container_env(container: str, name: str) -> str | None:
    """Value of environment variable ``name`` inside ``container``, or None if unset."""
    result = runner.run(
        ["docker", "exec", container, "printenv", name], timeout=COMMAND_TIMEOUT_S, check=False
    )
    if result.returncode == 1:
        return None
    if not result.ok:
        raise DrkitError(
            f"cannot read {name} from container {container}: "
            f"{runner.failure_detail(result, max_lines=1)}"
        )
    return result.stdout.rstrip("\n")


def exec_args(
    container: str, command: Sequence[str], *, env_names: Sequence[str] = (), stdin: bool = False
) -> list[str]:
    """argv for ``docker exec``. ``env_names`` are forwarded by name: their values come from
    drkit's environment, so they never appear on a command line."""
    args = ["docker", "exec"]
    if stdin:
        args.append("-i")
    for env_name in env_names:
        args += ["-e", env_name]
    return [*args, container, *command]


def has_executable(container: str, executable: str) -> bool:
    """True if ``executable --version`` runs inside ``container``."""
    result = runner.run(
        ["docker", "exec", container, executable, "--version"],
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    return result.ok
