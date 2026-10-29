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
        self.compose_project = project.compose_project
        self.volume = volume

    def tag_labels(self) -> dict[str, str]:
        return {"volume": self.name}

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        volume = docker.compose_volume(self.compose_project, self.name)
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
