"""A backup run (DESIGN §5.1): lock → sources → run manifest → retention → state → notify."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from drkit import __version__, lock, state
from drkit.config import LoadedConfig, Manifest, Retention
from drkit.errors import ConfigError, DrkitError, ExitCode
from drkit.notify import notify
from drkit.restic import Restic
from drkit.runmanifest import (
    META_KIND,
    RunManifest,
    manifest_filename,
    manifest_tags,
    new_run_id,
    run_status,
)
from drkit.runner import redact
from drkit.sources.base import BackupContext, ItemResult, Source
from drkit.sources.mysql import MysqlSource
from drkit.sources.paths import SystemPathsSource
from drkit.sources.postgres import PostgresSource
from drkit.sources.project import ProjectSource
from drkit.sources.secrets import SecretsSource
from drkit.sources.system import SystemInventorySource
from drkit.sources.volume import VolumeSource

SUPPORTED_KINDS = ("db", "volume", "secrets", "project", "system")

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    removed_runs: list[str]
    forgotten_snapshots: int


@dataclass(slots=True)
class BackupReport:
    manifest: RunManifest
    retention: RetentionResult | None = None
    errors: list[str] = field(default_factory=list)
    """Post-backup problems (retention, state file) that leave the backed-up data intact."""
    notify_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.manifest.status == "ok" and not self.errors

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.ok else ExitCode.PARTIAL_FAILURE


def restic_for(manifest: Manifest, env: dict[str, str]) -> Restic:
    return Restic(manifest.repository, host=manifest.host, env=env)


def build_sources(
    manifest: Manifest, *, project: str | None = None, kinds: Collection[str] | None = None
) -> list[Source]:
    """Items to back up, in manifest order, filtered by ``--project`` and ``--only``."""
    if kinds is not None:
        unknown = sorted(set(kinds) - set(SUPPORTED_KINDS))
        if unknown:
            raise ConfigError(
                f"unsupported item kind(s): {', '.join(unknown)}",
                hint=f"available: {', '.join(SUPPORTED_KINDS)}",
            )
    projects = [manifest.project(project)] if project else manifest.projects
    sources: list[Source] = []
    wanted = set(SUPPORTED_KINDS if kinds is None else kinds)
    for proj in projects:
        if "db" in wanted:
            for db in proj.databases:
                source_class = PostgresSource if db.engine == "postgres" else MysqlSource
                sources.append(source_class(proj, db))
        if "volume" in wanted:
            sources += [VolumeSource(proj, volume) for volume in proj.volumes]
        if "secrets" in wanted and proj.secrets:
            sources.append(SecretsSource(proj))
        if "project" in wanted:
            sources.append(ProjectSource(proj))
    # Host items belong to no project: a run limited to one project skips them.
    if "system" in wanted and project is None:
        if manifest.system.paths:
            sources.append(SystemPathsSource(manifest.system.paths))
        sources.append(SystemInventorySource())
    return sources


def run_backup(
    loaded: LoadedConfig,
    *,
    project: str | None = None,
    kinds: Collection[str] | None = None,
    restic: Restic | None = None,
    lock_path: Path | None = None,
    state_directory: Path | None = None,
    http_client: httpx.Client | None = None,
) -> BackupReport:
    manifest = loaded.manifest
    repo = restic or restic_for(manifest, dict(loaded.env))

    with lock.acquire(lock_path):
        started = datetime.now(UTC)
        run_id = new_run_id(started)
        with structlog.contextvars.bound_contextvars(run_id=run_id):
            log.info("backup.start", host=manifest.host, project=project, kinds=kinds)
            try:
                repo.ensure_ready()
                sources = build_sources(manifest, project=project, kinds=kinds)
            except DrkitError as exc:
                _notify_abort(manifest, f"could not start: {exc.message}", http_client)
                raise

            ctx = BackupContext(restic=repo, run_id=run_id, host=manifest.host)
            items = [_backup_item(source, ctx) for source in sources]
            run = RunManifest(
                run_id=run_id,
                host=manifest.host,
                drkit_version=__version__,
                started_at=started,
                finished_at=datetime.now(UTC),
                status=run_status(items),
                items=items,
                projects={
                    source.config.name: source.state
                    for source in sources
                    if isinstance(source, ProjectSource) and source.state is not None
                },
            )
            try:
                repo.backup_bytes(
                    run.to_json(),
                    manifest_filename(run_id),
                    manifest_tags(manifest.host, run_id, run.status),
                )
            except DrkitError as exc:
                # Without its manifest the run cannot be restored as a consistent set.
                _notify_abort(
                    manifest, f"run {run_id}: cannot store run manifest: {exc.message}", http_client
                )
                raise

            report = BackupReport(manifest=run)
            try:
                report.retention = apply_retention(repo, manifest.retention, protect=run_id)
            except DrkitError as exc:
                report.errors.append(f"retention: {exc.message}")
            try:
                state.record_run(run, state_directory)
            except DrkitError as exc:
                report.errors.append(f"state: {exc.message}")

            log.info(
                "backup.finish",
                status=run.status,
                items=len(items),
                failed=len(run.failed_items),
                duration_s=_duration(run),
            )
            report.notify_errors = notify(
                manifest.notifications,
                "success" if report.ok else "failure",
                format_message(manifest.host, report),
                client=http_client,
            )
    return report


def apply_retention(restic: Restic, retention: Retention, *, protect: str) -> RetentionResult:
    """Keep or drop whole runs (ADR-0004).

    The policy is evaluated on the run manifests (``kind:meta``, one per run). Every snapshot
    of a dropped run is then forgotten, so a kept manifest never points at a forgotten
    snapshot. ``protect`` (the current run) is never dropped.
    """
    _, remove = restic.retention_plan(retention, ["drkit", f"kind:{META_KIND}"])
    runs = sorted({r for s in remove if (r := s.tag_value("run")) and r != protect})
    if not runs:
        return RetentionResult(removed_runs=[], forgotten_snapshots=0)
    snapshots = restic.snapshots(*(["drkit", f"run:{run}"] for run in runs))
    forgotten = restic.forget(s.id for s in snapshots)
    log.info("retention.applied", removed_runs=len(runs), forgotten_snapshots=forgotten)
    return RetentionResult(removed_runs=runs, forgotten_snapshots=forgotten)


def format_message(host: str, report: BackupReport) -> str:
    run = report.manifest
    failed = run.failed_items
    lines = [
        f"drkit backup on {host}: {run.status.upper()}",
        f"run {run.run_id} · {len(run.items) - len(failed)} ok, {len(failed)} failed"
        f" · {_duration(run):.0f}s",
    ]
    lines += [f"✗ {item.label}: {item.error}" for item in failed]
    lines += [f"✗ {error}" for error in report.errors]
    return "\n".join(lines)


def _backup_item(source: Source, ctx: BackupContext) -> ItemResult:
    try:
        item = source.backup(ctx)
    except Exception as exc:
        # A bug in one source must not cost the other items their backup (DESIGN §5.1).
        log.exception("backup.item_crashed", kind=source.kind, name=source.name)
        item = ItemResult(
            kind=source.kind,
            project=source.project,
            name=source.name,
            status="failed",
            error=redact(f"unexpected error: {type(exc).__name__}: {exc}"),
        )
    log.info(
        "backup.item",
        item=item.label,
        status=item.status,
        bytes=item.bytes,
        duration_s=item.duration_s,
    )
    return item


def _notify_abort(manifest: Manifest, reason: str, client: httpx.Client | None) -> None:
    notify(
        manifest.notifications,
        "failure",
        f"drkit backup on {manifest.host}: FAILED\n{reason}",
        client=client,
    )


def _duration(run: RunManifest) -> float:
    return round((run.finished_at - run.started_at).total_seconds(), 1)
