"""Source protocol, item results and the streaming dump primitive shared by database sources."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict

from drkit import docker, runner
from drkit.config import Database, Project
from drkit.errors import CommandError, DrkitError
from drkit.restic import BACKUP_TIMEOUT_S, BackupSummary, Restic, parse_backup_summary

log = structlog.get_logger(__name__)

ItemStatus = Literal["ok", "failed"]


class ItemResult(BaseModel):
    """Outcome of backing up one item; stored as-is in the run manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    project: str | None = None
    name: str
    engine: str | None = None
    engine_version: str | None = None
    snapshot: str | None = None
    globals_snapshot: str | None = None
    bytes: int = 0
    duration_s: float = 0.0
    status: ItemStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BackupContext:
    restic: Restic
    run_id: str
    host: str

    def tags(self, kind: str, **labels: str | None) -> list[str]:
        """Snapshot tags (DESIGN §5.3): ``drkit``, ``host:``, ``run:``, ``kind:`` + labels."""
        tags = ["drkit", f"host:{self.host}", f"run:{self.run_id}", f"kind:{kind}"]
        tags += [f"{key}:{value}" for key, value in labels.items() if value]
        return tags


class Source(Protocol):
    kind: str
    project: str | None
    name: str

    def tags(self, ctx: BackupContext) -> list[str]: ...

    def backup(self, ctx: BackupContext) -> ItemResult: ...


class DumpFailedError(CommandError):
    """A dump command failed; any snapshot restic stored from its output was discarded."""


def stream_dump(
    ctx: BackupContext,
    producer: Sequence[str],
    *,
    label: str,
    filename: str,
    tags: Sequence[str],
    producer_env: Mapping[str, str] | None = None,
    timeout: float = BACKUP_TIMEOUT_S,
) -> BackupSummary:
    """Stream ``producer``'s stdout into a new restic snapshot without a temporary file.

    Both exit codes are checked (ADR-0005). restic happily stores a truncated stream, so if
    the dump fails after restic created a snapshot, that snapshot is forgotten at once and
    DumpFailedError is raised: a partial dump must never look like a backup.
    """
    result = runner.pipe(
        producer,
        ctx.restic.backup_stdin_command(filename, tags),
        timeout=timeout,
        producer_env=producer_env,
        consumer_env=ctx.restic.env,
    )
    # Look for a snapshot whatever restic's exit code: on an empty stream (a dump that died
    # before writing) restic exits 3 ("source could not be read") but still saves a snapshot.
    try:
        summary: BackupSummary | None = parse_backup_summary(result.consumer.stdout)
    except CommandError:
        summary = None

    if result.ok and summary is not None:
        return summary

    if summary is not None:
        ctx.restic.forget([summary.snapshot_id])
        log.warning("dump.discarded", file=filename, snapshot=summary.snapshot_id)

    if not result.producer.ok:
        detail = runner.failure_detail(result.producer, max_lines=3)
        raise DumpFailedError(
            f"{label} failed with exit code {result.producer.returncode}"
            + (f": {detail}" if detail else "")
        )
    if result.consumer.ok:
        raise CommandError(f"restic backup of {filename} did not report a snapshot")
    detail = runner.failure_detail(result.consumer, max_lines=3)
    raise CommandError(
        f"restic backup of {filename} failed with exit code {result.consumer.returncode}"
        + (f": {detail}" if detail else ""),
        hint="check repository access with `drkit doctor`",
    )


class DatabaseSource(ABC):
    """Template for engines: resolve the container, dump, and turn errors into a failed item."""

    kind = "db"
    engine: str

    def __init__(self, project: Project, database: Database) -> None:
        self.project: str | None = project.name
        self.compose_project = project.compose_project
        self.database = database
        self.name = database.database

    def tags(self, ctx: BackupContext) -> list[str]:
        return ctx.tags(self.kind, project=self.project, db=self.name)

    def filename(self, extension: str) -> str:
        return f"{self.project}/db/{self.name}.{extension}"

    def backup(self, ctx: BackupContext) -> ItemResult:
        started = time.monotonic()
        common: dict[str, Any] = {
            "kind": self.kind,
            "project": self.project,
            "name": self.name,
            "engine": self.engine,
        }
        try:
            container = docker.compose_container(self.compose_project, self.database.service)
            fields = self.dump(ctx, container)
        except DrkitError as exc:
            log.error(
                "backup.item_failed", item=f"{self.project}/db/{self.name}", error=exc.message
            )
            return ItemResult(
                **common,
                status="failed",
                error=runner.redact(exc.message),
                duration_s=round(time.monotonic() - started, 3),
            )
        return ItemResult(
            **common, **fields, status="ok", duration_s=round(time.monotonic() - started, 3)
        )

    @abstractmethod
    def dump(self, ctx: BackupContext, container: str) -> dict[str, Any]:
        """Back up the database; return ItemResult fields (snapshot, bytes, version, …)."""

    def require_env(self, container: str, name: str) -> str:
        value = docker.container_env(container, name)
        if not value:
            raise DrkitError(
                f"environment variable {name} is not set in the {self.database.service!r} "
                "container",
                hint="set it in the compose file, or point user_env / password_env at the "
                "variable that holds it",
            )
        return value
