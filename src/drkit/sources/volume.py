"""Docker volumes: ``restic backup <mountpoint>``, optionally with the services using it stopped."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from drkit import docker
from drkit.config import Project, Volume
from drkit.errors import DrkitError
from drkit.restic import BackupSummary
from drkit.sources.base import BackupContext, BaseSource

log = structlog.get_logger(__name__)


class VolumeSource(BaseSource):
    kind = "volume"

    def __init__(self, project: Project, volume: Volume) -> None:
        super().__init__(project=project.name, name=volume.name)
        self.config = project
        self.volume = volume

    def tag_labels(self) -> dict[str, str]:
        return {"volume": self.name}

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        volume = resolve(self.config, self.name)
        mountpoint = docker.volume_mountpoint(volume)
        # Checked before stopping anything: services must not go down for a backup that
        # cannot read the data anyway.
        if not readable(mountpoint):
            raise DrkitError(
                f"cannot read volume {volume} at {mountpoint}",
                hint="run drkit as root on the Docker host: volume data must be reachable "
                "from the host filesystem (Docker Desktop keeps it inside its VM)",
            )

        if self.volume.consistency == "live":
            summary = ctx.restic.backup_paths([mountpoint], self.tags(ctx))
            return {"snapshot": summary.snapshot_id, "bytes": summary.bytes_processed}

        stored: BackupSummary | None = None
        try:
            with stopped(docker.volume_containers(volume), label=self.label):
                stored = ctx.restic.backup_paths([mountpoint], self.tags(ctx))
        except DrkitError:
            # A failed item never keeps a snapshot, even when only the restart failed.
            if stored is not None:
                ctx.restic.forget([stored.snapshot_id])
            raise
        return {"snapshot": stored.snapshot_id, "bytes": stored.bytes_processed}


def resolve(project: Project, name: str, directory: Path | None = None) -> str:
    """Docker name of volume ``name`` of ``project``.

    Volumes are found by the labels Compose puts on the volumes it creates. External volumes
    have none: when the lookup fails, the compose files (in ``directory``, by default the
    project directory) tell whether that is why, so the error can say so.
    """
    try:
        return docker.compose_volume(project.compose_project, name)
    except DrkitError:
        if name in _external_volumes(project, directory or project.path):
            raise external_volume_error(project.name, name) from None
        raise


def external_volume_error(project: str, volume: str) -> DrkitError:
    return DrkitError(
        f"volume {volume!r} of project {project!r} is declared external in its compose files: "
        "drkit only backs up and restores volumes that Compose creates",
        hint="let Compose manage the volume (remove `external: true`), or remove it from "
        "projects[].volumes and back up its data another way",
    )


def _external_volumes(project: Project, directory: Path) -> set[str]:
    try:
        config = docker.compose_config(
            project.compose_project, project.compose_files(directory), directory
        )
    except DrkitError:
        return set()  # no usable compose files: the original error says enough
    return docker.external_volumes(config)


def readable(path: str) -> bool:
    return Path(path).is_dir() and os.access(path, os.R_OK | os.X_OK)


@contextmanager
def stopped(containers: Sequence[str], *, label: str) -> Iterator[None]:
    """Stop ``containers`` for the duration of the block; always start them again.

    ``docker start`` also runs after a failed or partial ``docker stop``: starting a running
    container is a no-op, and leaving a stopped one behind is the outage to avoid.
    """
    if not containers:
        yield
        return
    log.info("volume.stopping", item=label, containers=len(containers))
    try:
        docker.stop_containers(containers)
        yield
    finally:
        try:
            docker.start_containers(containers)
        except DrkitError as exc:
            log.error("volume.restart_failed", item=label, error=exc.message)
            raise DrkitError(
                f"services stopped for the backup did not start again: {exc.message}",
                hint="start them by hand (`docker start` / `docker compose up -d`) and check "
                "their logs",
            ) from exc
        log.info("volume.restarted", item=label, containers=len(containers))
