"""``drkit verify`` (DESIGN §6): repository integrity, then a real restore of every database.

Each database dump of a run is restored into a throwaway container of the same engine and
version line, with no network, and checked with sanity queries. A backup that cannot be
restored is found here, not during an outage.
"""

from __future__ import annotations

import re
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
import structlog

from drkit import runner, state
from drkit.config import LoadedConfig, Manifest
from drkit.errors import CommandError, DrkitError, ExitCode
from drkit.notify import notify
from drkit.orchestrator import restic_for
from drkit.restic import BACKUP_TIMEOUT_S, Restic
from drkit.runmanifest import load_run
from drkit.sources.base import ItemResult, dump_filename

DEFAULT_READ_DATA_SUBSET = "5%"
# restic's accepted forms: a percentage, a fraction n/t, or a size with K/M/G/T suffix.
READ_DATA_SUBSET_RE = re.compile(r"^(\d+(\.\d+)?%|\d+/\d+|\d+[KMGT]?)$")
CONTAINER_LABEL = "drkit.verify"
PULL_TIMEOUT_S = 900.0
READY_TIMEOUT_S = 180.0
COMMAND_TIMEOUT_S = 300.0

CheckStatus = Literal["ok", "failed"]

log = structlog.get_logger(__name__)
sleep = time.sleep  # replaced in tests


@dataclass(frozen=True, slots=True)
class DatabaseCheck:
    label: str
    engine: str
    image: str | None
    status: CheckStatus
    tables: int | None = None
    duration_s: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class VerifyReport:
    run_id: str
    repository_error: str | None
    databases: list[DatabaseCheck] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    """Problems recording the result (state file); the verification itself is unaffected."""
    notify_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.repository_error is None
            and all(db.status == "ok" for db in self.databases)
            and not self.errors
        )

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.ok else ExitCode.PARTIAL_FAILURE


def run_verify(
    loaded: LoadedConfig,
    *,
    run_id: str = "latest",
    read_data_subset: str | None = DEFAULT_READ_DATA_SUBSET,
    restic: Restic | None = None,
    state_directory: Path | None = None,
    http_client: httpx.Client | None = None,
) -> VerifyReport:
    manifest = loaded.manifest
    repo = restic or restic_for(manifest, dict(loaded.env))
    try:
        repo.ensure_ready()
        run = load_run(repo, run_id)
    except DrkitError as exc:
        _notify(manifest, f"drkit verify on {manifest.host}: FAILED\n{exc.message}", http_client)
        raise

    with structlog.contextvars.bound_contextvars(run_id=run.run_id):
        log.info("verify.start", read_data_subset=read_data_subset)
        report = VerifyReport(run_id=run.run_id, repository_error=None)
        try:
            repo.check(read_data_subset)
        except DrkitError as exc:
            report.repository_error = exc.message
            log.error("verify.check_failed", error=exc.message)

        for item in run.items:
            if item.kind == "db" and item.status == "ok":
                report.databases.append(verify_database(repo, item, _verify_query(manifest, item)))

        try:
            state.record_verify(run.run_id, "ok" if report.ok else "failed", state_directory)
        except DrkitError as exc:
            report.errors.append(f"state: {exc.message}")
        log.info("verify.finish", ok=report.ok, databases=len(report.databases))
        report.notify_errors = _notify(
            manifest,
            format_message(manifest.host, report),
            http_client,
            event="success" if report.ok else "failure",
        )
    return report


def verify_database(restic: Restic, item: ItemResult, query: str | None) -> DatabaseCheck:
    """Restore one database dump into a throwaway container and check it."""
    started = time.monotonic()
    engine: Engine = Postgres() if item.engine == "postgres" else Mysql(item.engine_version)
    image: str | None = None
    container = f"drkit-verify-{secrets.token_hex(4)}"
    try:
        image = engine.image(item.engine_version)
        log.info("verify.database", item=item.label, image=image)
        runner.run(["docker", "pull", "--quiet", image], timeout=PULL_TIMEOUT_S)
        runner.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container,
                "--label",
                f"{CONTAINER_LABEL}=true",
                # The restored data is a copy of production: nothing may reach it.
                "--network",
                "none",
                *_env_args(engine.env(item.name)),
                image,
            ],
            timeout=COMMAND_TIMEOUT_S,
        )
        _wait_ready(container, engine.ready_command())
        engine.restore(restic, container, item)
        tables = engine.count_tables(container, item.name)
        if tables == 0:
            raise DrkitError("the restored database has no tables")
        if query:
            engine.query(container, item.name, query)
    except DrkitError as exc:
        log.error("verify.database_failed", item=item.label, error=exc.message)
        return DatabaseCheck(
            label=item.label,
            engine=item.engine or "?",
            image=image,
            status="failed",
            duration_s=round(time.monotonic() - started, 1),
            error=runner.redact(exc.message),
        )
    finally:
        _remove(container)
    return DatabaseCheck(
        label=item.label,
        engine=item.engine or "?",
        image=image,
        status="ok",
        tables=tables,
        duration_s=round(time.monotonic() - started, 1),
    )


# --------------------------------------------------------------------------- engines


class Engine(ABC):
    """How to start, restore into and query a throwaway server of one engine."""

    @abstractmethod
    def image(self, version: str | None) -> str: ...

    @abstractmethod
    def env(self, database: str) -> list[tuple[str, str]]: ...

    @abstractmethod
    def ready_command(self) -> list[str]: ...

    @abstractmethod
    def restore(self, restic: Restic, container: str, item: ItemResult) -> None: ...

    @abstractmethod
    def count_tables(self, container: str, database: str) -> int: ...

    @abstractmethod
    def query(self, container: str, database: str, query: str) -> str: ...


class Postgres(Engine):
    # Trust is safe here: the container has no network and lives for the check only.
    ENV = (("POSTGRES_HOST_AUTH_METHOD", "trust"),)

    def image(self, version: str | None) -> str:
        # Since PostgreSQL 10 the first number is the major version; dumps restore into the
        # same major (or newer), so the same major is the faithful test.
        return f"postgres:{_version_parts(version, 1)}"

    def env(self, database: str) -> list[tuple[str, str]]:
        return list(self.ENV)

    def ready_command(self) -> list[str]:
        # TCP on purpose: during initialization the server only listens on the socket.
        return ["pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-q"]

    def restore(self, restic: Restic, container: str, item: ItemResult) -> None:
        if item.globals_snapshot:
            # Roles first, so the restored objects keep their owners and grants. The bootstrap
            # role already exists: psql reports it and carries on.
            _pipe(
                restic,
                item.globals_snapshot,
                dump_filename(item.project, item.name, "globals.sql"),
                [container, "psql", "-U", "postgres", "-X", "-q", "-d", "postgres"],
                label="psql (roles)",
            )
        _exec(container, ["createdb", "-U", "postgres", item.name])
        assert item.snapshot is not None  # noqa: S101 - ok items always have one
        _pipe(
            restic,
            item.snapshot,
            dump_filename(item.project, item.name, "dump"),
            [container, "pg_restore", "-U", "postgres", "-d", item.name, "--exit-on-error"],
            label="pg_restore",
        )

    def count_tables(self, container: str, database: str) -> int:
        return int(
            self.query(
                container,
                database,
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')",
            )
        )

    def query(self, container: str, database: str, query: str) -> str:
        return _exec(
            container,
            ["psql", "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1", "-XtAc", query],
        ).strip()


class Mysql(Engine):
    def __init__(self, version: str | None) -> None:
        self.mariadb = "mariadb" in (version or "").lower()
        self.client = "mariadb" if self.mariadb else "mysql"

    def image(self, version: str | None) -> str:
        # Major.minor: MySQL 8.0 and 8.4, or MariaDB 10.11 and 11.4, are different lines.
        return f"{self.client}:{_version_parts(version, 2)}"

    def env(self, database: str) -> list[tuple[str, str]]:
        # An empty root password is safe here: the container has no network.
        if self.mariadb:
            return [("MARIADB_ALLOW_EMPTY_ROOT_PASSWORD", "yes"), ("MARIADB_DATABASE", database)]
        return [("MYSQL_ALLOW_EMPTY_PASSWORD", "yes"), ("MYSQL_DATABASE", database)]

    def ready_command(self) -> list[str]:
        admin = "mariadb-admin" if self.mariadb else "mysqladmin"
        # TCP on purpose: the entrypoint's temporary server runs with networking disabled.
        return [admin, "ping", "-h", "127.0.0.1", "-uroot", "--silent"]

    def restore(self, restic: Restic, container: str, item: ItemResult) -> None:
        assert item.snapshot is not None  # noqa: S101 - ok items always have one
        _pipe(
            restic,
            item.snapshot,
            dump_filename(item.project, item.name, "sql"),
            [container, self.client, "-uroot", item.name],
            label=self.client,
        )

    def count_tables(self, container: str, database: str) -> int:
        # Database names are validated ([A-Za-z0-9_-]) when the manifest is loaded.
        statement = "SELECT count(*) FROM information_schema.tables WHERE table_schema = '{}'"
        return int(self.query(container, database, statement.format(database)))

    def query(self, container: str, database: str, query: str) -> str:
        return _exec(
            container,
            [self.client, "-uroot", "--batch", "--skip-column-names", database, "-e", query],
        ).strip()


# --------------------------------------------------------------------------- helpers


def _env_args(env: Sequence[tuple[str, str]]) -> list[str]:
    # Plain settings, no secrets: fine on the command line.
    return [arg for name, value in env for arg in ("-e", f"{name}={value}")]


def _remove(container: str) -> None:
    # Also stops it; a container that never started is "not found", which is fine.
    try:
        runner.run(["docker", "rm", "--force", container], timeout=COMMAND_TIMEOUT_S, check=False)
    except DrkitError as exc:
        log.warning("verify.cleanup_failed", container=container, error=exc.message)


def _version_parts(version: str | None, count: int) -> str:
    match = re.match(r"^(\d+)(?:\.(\d+))?", version or "")
    if match is None or (count == 2 and match.group(2) is None):
        raise DrkitError(f"cannot tell the server version from {version!r}")
    return ".".join(match.groups()[:count])


def _exec(container: str, command: Sequence[str]) -> str:
    result = runner.run(
        ["docker", "exec", container, *command], timeout=COMMAND_TIMEOUT_S, check=False
    )
    if not result.ok:
        raise CommandError(
            f"{command[0]} failed with exit code {result.returncode}: "
            f"{runner.failure_detail(result, max_lines=3)}"
        )
    return result.stdout


def _pipe(
    restic: Restic, snapshot: str, filename: str, consumer: Sequence[str], *, label: str
) -> None:
    result = runner.pipe(
        restic.dump_command(snapshot, f"/{filename}"),
        ["docker", "exec", "-i", *consumer],
        timeout=BACKUP_TIMEOUT_S,
        producer_env=restic.env,
    )
    if not result.producer.ok:
        raise CommandError(
            f"restic dump of {filename} failed with exit code {result.producer.returncode}: "
            f"{runner.failure_detail(result.producer, max_lines=3)}"
        )
    if not result.consumer.ok:
        raise CommandError(
            f"{label} failed with exit code {result.consumer.returncode}: "
            f"{runner.failure_detail(result.consumer, max_lines=3)}"
        )


def _wait_ready(container: str, probe: Sequence[str]) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        result = runner.run(
            ["docker", "exec", container, *probe], timeout=COMMAND_TIMEOUT_S, check=False
        )
        if result.ok:
            return
        if "is not running" in result.stderr or time.monotonic() > deadline:
            logs = runner.run(
                ["docker", "logs", "--tail", "5", container], timeout=COMMAND_TIMEOUT_S, check=False
            )
            raise CommandError(
                "the verification database did not start: "
                f"{runner.failure_detail(logs, max_lines=5) or 'no output'}"
            )
        sleep(1.0)


def _verify_query(manifest: Manifest, item: ItemResult) -> str | None:
    """``verify_query`` of the database in the current manifest, if it is still there."""
    for project in manifest.projects:
        if project.name == item.project:
            for database in project.databases:
                if database.database == item.name:
                    return database.verify_query
    return None


def format_message(host: str, report: VerifyReport) -> str:
    restored = sum(db.status == "ok" for db in report.databases)
    lines = [
        f"drkit verify on {host}: {'OK' if report.ok else 'FAILED'}",
        f"run {report.run_id} · repository check "
        f"{'ok' if report.repository_error is None else 'FAILED'}"
        f" · {restored}/{len(report.databases)} database(s) restored",
    ]
    if report.repository_error:
        lines.append(f"✗ repository: {report.repository_error}")
    lines += [f"✗ {db.label}: {db.error}" for db in report.databases if db.status == "failed"]
    lines += [f"✗ {error}" for error in report.errors]
    return "\n".join(lines)


def _notify(
    manifest: Manifest,
    message: str,
    client: httpx.Client | None,
    *,
    event: Literal["success", "failure"] = "failure",
) -> list[str]:
    return notify(manifest.notifications, event, message, client=client)
