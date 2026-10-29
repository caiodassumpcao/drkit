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

    @property
    def label(self) -> str:
        return item_label(self.kind, self.project, self.name)


# Kinds with a single item per project, named after it: their label omits the name.
PER_PROJECT_KINDS = frozenset({"project", "secrets"})


def item_label(kind: str, project: str | None, name: str) -> str:
    """``shop/db/orders``, ``shop/project``, ``system/paths``."""
    parts = [project, kind] if kind in PER_PROJECT_KINDS else [project, kind, name]
    return "/".join(part for part in parts if part)


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


def dump_filename(project: str | None, database: str, extension: str) -> str:
    """Path of a database dump inside its snapshot, without the leading ``/``."""
    return f"{project}/db/{database}.{extension}"


class BaseSource(ABC):
    """Template for sources: time the item and turn expected failures into a failed item."""

    kind: str

    def __init__(self, *, project: str | None, name: str) -> None:
        self.project = project
        self.name = name

    @property
    def label(self) -> str:
        return item_label(self.kind, self.project, self.name)

    def tag_labels(self) -> dict[str, str]:
        """Kind-specific tags (DESIGN §5.3), e.g. ``{"db": "orders"}``."""
        return {}

    def tags(self, ctx: BackupContext) -> list[str]:
        return ctx.tags(self.kind, project=self.project, **self.tag_labels())

    def result_fields(self) -> dict[str, Any]:
        """Fields recorded even when the item fails (e.g. the database engine)."""
        return {}

    def backup(self, ctx: BackupContext) -> ItemResult:
        started = time.monotonic()
        common = {
            "kind": self.kind,
            "project": self.project,
            "name": self.name,
            **self.result_fields(),
        }
        try:
            fields = self.run(ctx)
        except DrkitError as exc:
            log.error("backup.item_failed", item=self.label, error=exc.message)
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
    def run(self, ctx: BackupContext) -> dict[str, Any]:
        """Back up the item; return ItemResult fields (snapshot, bytes, …)."""


class DatabaseSource(BaseSource):
    """Template for engines: resolve the service container, then dump the database."""

    kind = "db"
    engine: str

    def __init__(self, project: Project, database: Database) -> None:
        super().__init__(project=project.name, name=database.database)
        self.compose_project = project.compose_project
        self.database = database

    def tag_labels(self) -> dict[str, str]:
        return {"db": self.name}

    def result_fields(self) -> dict[str, Any]:
        return {"engine": self.engine}

    def filename(self, extension: str) -> str:
        return dump_filename(self.project, self.name, extension)

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        container = docker.compose_container(self.compose_project, self.database.service)
        return self.dump(ctx, container)

    @abstractmethod
    def dump(self, ctx: BackupContext, container: str) -> dict[str, Any]:
        """Back up the database; return ItemResult fields (snapshot, bytes, version, …)."""

    def require_env(self, container: str, names: Sequence[str]) -> str:
        """Value of the first of ``names`` set (and non-empty) in the container."""
        for name in names:
            if value := docker.container_env(container, name):
                return value
        what = (
            f"environment variable {names[0]} is not set"
            if len(names) == 1
            else f"none of the environment variables {', '.join(names)} is set"
        )
        raise DrkitError(
            f"{what} in the {self.database.service!r} container",
            hint="set it in the compose file, or point user_env / password_env at the "
            "variable that holds it",
        )
