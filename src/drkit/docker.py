"""Docker and Compose access through the CLI (ADR-0003).

Containers are found by the labels Compose sets on them, so drkit needs neither the compose
files nor the project directory to locate a service.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import structlog

from drkit import runner
from drkit.errors import DrkitError

COMMAND_TIMEOUT_S = 60.0
# Seconds `docker stop` waits for a graceful shutdown before SIGKILL (Docker's default is 10).
STOP_GRACE_S = 30


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


def compose_volume(project: str, volume: str) -> str:
    """Docker name of volume ``volume`` of compose ``project`` (e.g. ``shop_uploads``)."""
    result = runner.run(
        [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.volume={volume}",
        ],
        timeout=COMMAND_TIMEOUT_S,
    )
    names = sorted(result.stdout.split())
    if not names:
        raise DrkitError(
            f"volume {volume!r} of compose project {project!r} not found",
            hint="create it with `docker compose up`, or fix volumes[].name / "
            "compose.project_name in the manifest (external volumes are not supported)",
        )
    return names[0]


def volume_mountpoint(name: str) -> str:
    """Host directory holding the data of volume ``name``."""
    result = runner.run(
        ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", name],
        timeout=COMMAND_TIMEOUT_S,
    )
    return result.stdout.strip()


def volume_containers(name: str) -> list[str]:
    """IDs of the running containers that mount volume ``name``, ordered by name."""
    result = runner.run(
        ["docker", "ps", "--filter", f"volume={name}", "--format", "{{.Names}}\t{{.ID}}"],
        timeout=COMMAND_TIMEOUT_S,
    )
    rows = sorted(line.split("\t") for line in result.stdout.splitlines() if "\t" in line)
    return [container_id for _, container_id in rows]


def stop_containers(containers: Sequence[str]) -> None:
    runner.run(
        ["docker", "stop", "--time", str(STOP_GRACE_S), *containers],
        timeout=COMMAND_TIMEOUT_S + STOP_GRACE_S,
    )


def start_containers(containers: Sequence[str]) -> None:
    runner.run(["docker", "start", *containers], timeout=COMMAND_TIMEOUT_S)


def project_images(project: str) -> dict[str, str]:
    """Image of each running service of compose ``project``, pinned by registry digest.

    Values look like ``postgres:16.4@sha256:…``. An image without a registry digest (built
    locally) is recorded by its reference, with a warning: a restore will have to rebuild it.
    """
    ids = runner.run(
        ["docker", "ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}"],
        timeout=COMMAND_TIMEOUT_S,
    ).stdout.split()
    if not ids:
        return {}
    containers = runner.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.service"}}\t{{.Config.Image}}\t{{.Image}}',
            *ids,
        ],
        timeout=COMMAND_TIMEOUT_S,
    ).stdout.splitlines()
    services = sorted(tuple(line.split("\t")) for line in containers if line.count("\t") == 2)
    image_ids = sorted({image_id for _, _, image_id in services})
    digests: dict[str, list[str]] = {}
    for line in runner.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}\t{{json .RepoDigests}}", *image_ids],
        timeout=COMMAND_TIMEOUT_S,
    ).stdout.splitlines():
        image_id, _, repo_digests = line.partition("\t")
        digests[image_id] = json.loads(repo_digests or "null") or []

    images: dict[str, str] = {}
    for service, reference, image_id in services:
        if service in images:
            continue  # replicas run the same image
        pinned = pin_reference(reference, digests.get(image_id, []))
        if pinned is None:
            log.warning("docker.image_without_digest", project=project, service=service)
            pinned = reference
        images[service] = pinned
    return images


def pin_reference(reference: str, repo_digests: Sequence[str]) -> str | None:
    """``reference`` pinned to its registry digest, or None if the image has none.

    ``repo_digests`` come from ``docker image inspect`` (``postgres@sha256:…``).
    """
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    repository = name[: len(name) - len(last)] + last.split(":", 1)[0]
    candidates = [d for d in repo_digests if d.split("@", 1)[0] == repository] or repo_digests
    if not candidates:
        return None
    return f"{name}@{candidates[0].split('@', 1)[1]}"
