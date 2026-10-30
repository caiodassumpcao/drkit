"""``drkit restore db`` (DESIGN §7.1), also used by the database step of a project restore.

Dumps stream from restic into the service's own client (``pg_restore``, ``mysql``) with the
credentials the service already has: nothing is written to disk and no password ever
appears on a command line. Before overwriting anything, the current database is dumped into
a ``kind:pre-restore`` snapshot, so a restore that picked the wrong run can be undone.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from drkit import docker, runner
from drkit.config import Database, LoadedConfig, Project
from drkit.errors import ConfigError, DrkitError
from drkit.restic import Restic
from drkit.restore.steps import RestorePlan, Step
from drkit.runmanifest import RunManifest, find_run, new_run_id
from drkit.sources.base import BackupContext, ItemResult, dump_filename, require_env
from drkit.sources.mysql import PASSWORD_VAR, MysqlSource
from drkit.sources.postgres import PostgresSource

PRE_RESTORE_KIND = "pre-restore"
# Ample for a server that is still initializing an empty volume on first start.
READY_TIMEOUT_S = 300.0
COMMAND_TIMEOUT_S = 300.0


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    """A database dump of a run and the compose service it is restored into."""

    project: Project
    database: Database
    """Manifest entry; ``service`` is the ``--into`` service when one is given."""
    item: ItemResult

    @property
    def label(self) -> str:
        return self.item.label

    @property
    def service(self) -> str:
        return self.database.service


@dataclass(frozen=True, slots=True)
class SafetyContext(BackupContext):
    """Backup context for safety dumps; ``run_id`` holds the restore ID.

    Safety dumps carry no ``run:`` tag: they belong to no run, so neither retention nor
    ``drkit prune`` ever forgets them. They are removed by hand once no longer needed.
    """

    def tags(self, kind: str, **labels: str | None) -> list[str]:
        tags = ["drkit", f"host:{self.host}", f"kind:{PRE_RESTORE_KIND}", f"restore:{self.run_id}"]
        tags.append(f"item:{kind}")  # what the snapshot holds: db, volume, secrets, project
        return tags + [f"{key}:{value}" for key, value in labels.items() if value]


# --------------------------------------------------------------------------- engines


class Engine(ABC):
    """How to restore into, and query, the running database service of a target."""

    env_names: Sequence[str] = ()
    """Variables forwarded by name to every ``docker exec`` (values from ``env``)."""
    env: Mapping[str, str] | None = None

    def __init__(self, target: DatabaseTarget, container: str) -> None:
        self.target = target
        self.container = container
        self.name = target.item.name

    @abstractmethod
    def ready_probe(self) -> list[str]: ...

    @abstractmethod
    def ensure_database(self) -> None:
        """Create the database if the server does not have it (e.g. a fresh volume)."""

    def restore_roles(self, restic: Restic) -> None:  # noqa: B027 - optional step
        """Recreate cluster-wide objects (roles) the dump depends on."""

    @abstractmethod
    def restore(self, restic: Restic) -> None: ...

    @abstractmethod
    def count_tables(self) -> int: ...

    @abstractmethod
    def server_version(self) -> str: ...

    def check_version(self) -> str:
        """Fail early when the target server is older than the one the dump came from.

        Dumps restore into the same version line or a newer one; an older server fails
        halfway, after the safety dump and possibly after dropping tables.
        """
        dumped = self.target.item.engine_version or ""
        running = self.server_version()
        if _family(dumped) != _family(running):
            raise DrkitError(
                f"{self.target.label} was dumped from {dumped} but service "
                f"{self.target.service!r} runs {running}: a different server family",
                hint="restore into a service running the same server (MySQL or MariaDB)",
            )
        parts = 1 if self.target.database.engine == "postgres" else 2
        dumped_line, running_line = _line(dumped, parts), _line(running, parts)
        if dumped_line and running_line and running_line < dumped_line:
            raise DrkitError(
                f"{self.target.label} was dumped from {dumped}, newer than the {running} of "
                f"service {self.target.service!r}",
                hint="restore into a service of the same version line or newer",
            )
        return running

    @abstractmethod
    def query(self, statement: str) -> str: ...

    def wait_ready(self) -> None:
        docker.wait_ready(
            self.container,
            self.ready_probe(),
            timeout=READY_TIMEOUT_S,
            what=f"database service {self.target.service!r}",
            env_names=self.env_names,
            env=self.env,
        )

    def exec(self, command: Sequence[str]) -> str:
        return docker.exec_output(
            self.container,
            command,
            timeout=COMMAND_TIMEOUT_S,
            env_names=self.env_names,
            env=self.env,
        )


class PostgresEngine(Engine):
    def __init__(self, target: DatabaseTarget, container: str) -> None:
        super().__init__(target, container)
        # Local socket connections are trusted inside the official image: no password.
        self.user = require_env(
            container, target.database.user_env_candidates, service=target.service
        )

    def ready_probe(self) -> list[str]:
        # TCP on purpose: while the entrypoint initializes a fresh volume, the temporary
        # server only listens on the socket, and is stopped right after.
        return ["pg_isready", "-h", "127.0.0.1", "-U", self.user, "-d", "postgres", "-q"]

    def ensure_database(self) -> None:
        # Database names are validated ([A-Za-z0-9_-]) when the manifest is loaded.
        statement = f"SELECT 1 FROM pg_database WHERE datname = '{self.name}'"  # noqa: S608
        exists = self._psql("postgres", statement)
        if not exists:
            self.exec(["createdb", "-U", self.user, self.name])

    def restore_roles(self, restic: Restic) -> None:
        item = self.target.item
        if not item.globals_snapshot:
            return
        # Roles that already exist are reported by psql and left as they are; their
        # attributes and passwords are then set to the backed-up values.
        restic.dump_into(
            item.globals_snapshot,
            "/" + dump_filename(item.project, item.name, "globals.sql"),
            [
                *("docker", "exec", "-i", self.container),
                *("psql", "-U", self.user, "-X", "-q", "-d", "postgres"),
            ],
            label="psql (roles)",
        )

    def restore(self, restic: Restic) -> None:
        item = self.target.item
        assert item.snapshot is not None  # noqa: S101 - targets are ok items
        # One transaction: a failed restore leaves the database exactly as it was.
        restic.dump_into(
            item.snapshot,
            "/" + dump_filename(item.project, item.name, "dump"),
            [
                *("docker", "exec", "-i", self.container),
                *("pg_restore", "-U", self.user, "-d", self.name),
                *("--clean", "--if-exists", "--single-transaction"),
            ],
            label="pg_restore",
        )

    def count_tables(self) -> int:
        return int(
            self._psql(
                self.name,
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')",
            )
        )

    def query(self, statement: str) -> str:
        return self._psql(self.name, statement)

    def server_version(self) -> str:
        # e.g. "16.9 (Debian 16.9-1.pgdg120+1)"
        return self._psql("postgres", "SHOW server_version").split(" ", 1)[0]

    def _psql(self, database: str, statement: str) -> str:
        return self.exec(
            ["psql", "-U", self.user, "-d", database, "-v", "ON_ERROR_STOP=1", "-XtAc", statement]
        ).strip()


class MysqlEngine(Engine):
    def __init__(self, target: DatabaseTarget, container: str) -> None:
        super().__init__(target, container)
        database, service = target.database, target.service
        self.user = require_env(container, database.user_env_candidates, service=service)
        password = require_env(container, database.password_env_candidates, service=service)
        runner.redactor.add(password)
        # Handed to `docker exec -e MYSQL_PWD` through drkit's environment, never on argv.
        self.env_names = (PASSWORD_VAR,)
        self.env = {PASSWORD_VAR: password}
        # MariaDB 11 images no longer ship the mysql* client names.
        mariadb = docker.has_executable(container, "mariadb")
        self.client = "mariadb" if mariadb else "mysql"
        self.admin = "mariadb-admin" if mariadb else "mysqladmin"

    def ready_probe(self) -> list[str]:
        # TCP on purpose: the entrypoint's temporary init server runs without networking.
        return [self.admin, "ping", "-h", "127.0.0.1", "-u", self.user, "--silent"]

    def ensure_database(self) -> None:
        self.exec(
            [self.client, "-u", self.user, "-e", f"CREATE DATABASE IF NOT EXISTS `{self.name}`"]
        )

    def restore(self, restic: Restic) -> None:
        item = self.target.item
        assert item.snapshot is not None  # noqa: S101 - targets are ok items
        # The dump drops and recreates each of its tables (mysqldump's default).
        restic.dump_into(
            item.snapshot,
            "/" + dump_filename(item.project, item.name, "sql"),
            [
                *("docker", "exec", "-i", "-e", PASSWORD_VAR, self.container),
                *(self.client, "-u", self.user, self.name),
            ],
            label=self.client,
            consumer_env=self.env,
        )

    def server_version(self) -> str:
        return self.exec(
            [
                self.client,
                "-u",
                self.user,
                "--batch",
                "--skip-column-names",
                "-e",
                "SELECT VERSION()",
            ]
        ).strip()

    def count_tables(self) -> int:
        statement = "SELECT count(*) FROM information_schema.tables WHERE table_schema = '{}'"
        return int(self.query(statement.format(self.name)))

    def query(self, statement: str) -> str:
        return self.exec(
            [
                *(self.client, "-u", self.user, "--batch", "--skip-column-names"),
                *(self.name, "-e", statement),
            ]
        ).strip()


def connect(target: DatabaseTarget) -> Engine:
    """Engine for the running container of the target's service."""
    container = docker.compose_container(target.project.compose_project, target.service)
    if target.database.engine == "postgres":
        return PostgresEngine(target, container)
    return MysqlEngine(target, container)


# --------------------------------------------------------------------------- operations


def restore_database(restic: Restic, target: DatabaseTarget, *, roles: bool) -> str:
    """Restore one dump into its service. ``roles`` also recreates PostgreSQL roles."""
    engine = connect(target)
    engine.wait_ready()
    if roles:
        engine.restore_roles(restic)
    engine.ensure_database()
    engine.restore(restic)
    return f"{target.label} into service {target.service}"


def check_database(target: DatabaseTarget) -> str:
    """Sanity check after a restore: tables exist, and ``verify_query`` runs if configured."""
    engine = connect(target)
    tables = engine.count_tables()
    if tables == 0:
        raise DrkitError(f"{target.label}: the restored database has no tables")
    if target.database.verify_query:
        engine.query(target.database.verify_query)
        return f"{target.label}: {tables} table(s), verify_query ok"
    return f"{target.label}: {tables} table(s)"


def safety_dump(restic: Restic, target: DatabaseTarget, *, host: str, restore_id: str) -> str:
    """Dump the database as it is now into a ``kind:pre-restore`` snapshot."""
    source_class = PostgresSource if target.database.engine == "postgres" else MysqlSource
    source = source_class(target.project, target.database)
    container = docker.compose_container(target.project.compose_project, target.service)
    fields = source.dump(SafetyContext(restic=restic, run_id=restore_id, host=host), container)
    return f"{target.label}: snapshot {str(fields['snapshot'])[:8]}"


def database_targets(
    project: Project, run: RunManifest, *, only: str | None = None, into: str | None = None
) -> list[DatabaseTarget]:
    """The databases of ``project`` in ``run`` (or just ``only``), checked restorable."""
    items = [
        item
        for item in run.items
        if item.kind == "db" and item.project == project.name and only in (None, item.name)
    ]
    if only is not None and not items:
        raise DrkitError(
            f"run {run.run_id} has no backup of database {only!r} of project {project.name!r}",
            hint="pick another run with --run (see `drkit runs list`)",
        )
    configured = {database.database: database for database in project.databases}
    targets = []
    for item in items:
        if item.status != "ok":
            raise DrkitError(
                f"{item.label} failed in run {run.run_id} and cannot be restored from it",
                hint="pick another run with --run (see `drkit runs list`)",
            )
        database = configured.get(item.name)
        if database is None or database.engine != item.engine:
            raise ConfigError(
                f"{item.label} ({item.engine}) of run {run.run_id} is not in the manifest",
                hint="add it back to projects[].databases: the service and credentials to "
                "restore it with come from there",
            )
        if into is not None:
            database = database.model_copy(update={"service": into})
        targets.append(DatabaseTarget(project=project, database=database, item=item))
    return targets


def plan(
    loaded: LoadedConfig,
    project_name: str,
    restic: Restic,
    *,
    run_id: str = "latest",
    database: str | None = None,
    into: str | None = None,
    safety_backup: bool = True,
) -> RestorePlan:
    """Plan ``drkit restore db``: every database of the project, or just ``database``."""
    manifest = loaded.manifest
    project = manifest.project(project_name)
    if database is not None and database not in {d.database for d in project.databases}:
        known = ", ".join(d.database for d in project.databases) or "none"
        raise ConfigError(
            f"project {project.name!r} has no database {database!r}",
            hint=f"databases of {project.name}: {known}",
        )
    what = (
        f"database {database!r} of project {project.name!r}"
        if database
        else (f"the databases of project {project.name!r}")
    )
    run = find_run(
        restic,
        run_id,
        accept=lambda run: bool(_db_items(run, project.name, database)),
        what=what,
    )
    targets = database_targets(project, run, only=database, into=into)
    if not targets:
        raise DrkitError(
            f"run {run.run_id} has no database backup of project {project.name!r}",
            hint="pick another run with --run (see `drkit runs list`)",
        )
    restore_id = new_run_id()

    def locate() -> str:
        details = []
        for target in targets:
            for snapshot in (target.item.snapshot, target.item.globals_snapshot):
                if snapshot:
                    restic.snapshot(snapshot)
            version = connect(target).check_version()
            details.append(f"{target.label}: dump found, service {target.service} runs {version}")
        return "; ".join(details)

    def safety() -> str:
        if not safety_backup:
            return "not taken (--no-safety-backup)"
        return "; ".join(
            safety_dump(restic, t, host=manifest.host, restore_id=restore_id) for t in targets
        )

    def restore() -> str:
        return "; ".join(restore_database(restic, t, roles=False) for t in targets)

    def check() -> str:
        return "; ".join(check_database(t) for t in targets)

    steps = [
        Step(
            "Locate the dumps in the run",
            locate,
            tuple(
                f"{t.label}: snapshot {(t.item.snapshot or '')[:8]} "
                f"({t.item.engine} {t.item.engine_version or '?'}) → service {t.service}"
                for t in targets
            ),
        ),
        Step(
            "Safety dump of the current databases",
            safety,
            tuple(f"{t.label}: kind:{PRE_RESTORE_KIND} snapshot" for t in targets)
            if safety_backup
            else ("skipped (--no-safety-backup)",),
        ),
        Step("Restore the dumps", restore, tuple(_restore_description(t) for t in targets)),
        Step(
            "Sanity check",
            check,
            tuple(
                f"{t.label}: count tables"
                + (", run verify_query" if t.database.verify_query else "")
                for t in targets
            ),
        ),
    ]
    warnings = (
        []
        if safety_backup
        else ["no safety dump: the current data cannot be recovered after the restore"]
    )
    labels = ", ".join(t.label for t in targets)
    return RestorePlan(
        kind="db",
        target=labels,
        run_id=run.run_id,
        steps=steps,
        summary=f"Overwrite {labels} with the dump(s) of run {run.run_id}.",
        warnings=warnings,
        restore_id=restore_id,
    )


def _family(version: str) -> str:
    return "mariadb" if "mariadb" in version.lower() else "other"


def _line(version: str, parts: int) -> tuple[int, ...]:
    """``16.9`` → ``(16,)``; ``8.4.9`` → ``(8, 4)``; ``()`` when unparsable (not compared)."""
    match = re.match(r"^(\d+)(?:\.(\d+))?", version)
    if match is None:
        return ()
    numbers = [int(n) for n in match.groups() if n is not None]
    return tuple(numbers[:parts])


def _db_items(run: RunManifest, project: str, database: str | None) -> list[ItemResult]:
    return [
        item
        for item in run.items
        if item.kind == "db" and item.project == project and database in (None, item.name)
    ]


def _restore_description(target: DatabaseTarget) -> str:
    if target.database.engine == "postgres":
        return f"{target.label}: pg_restore --clean --if-exists --single-transaction"
    return f"{target.label}: dump replayed with the mysql client (its tables are recreated)"
