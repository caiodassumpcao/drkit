"""MySQL / MariaDB: ``mysqldump`` (or ``mariadb-dump``) streamed into restic."""

from __future__ import annotations

from typing import Any

from drkit import docker, runner
from drkit.sources.base import BackupContext, DatabaseSource, stream_dump

VERSION_TIMEOUT_S = 30.0
PASSWORD_VAR = "MYSQL_PWD"  # noqa: S105 - variable name read by MySQL and MariaDB clients

DUMP_OPTIONS = (
    "--single-transaction",  # consistent InnoDB snapshot without locking tables
    "--routines",
    "--triggers",
    "--events",
    "--no-tablespaces",  # otherwise MySQL >= 8.0.21 requires the global PROCESS privilege
)
# GTIDs are on by default in recent MySQL. Recording the GTID set needs FLUSH TABLES (RELOAD
# privilege) at dump time and SUPER to apply it at restore time; a logical restore does not
# need it. mariadb-dump has no such option.
MYSQL_ONLY_OPTIONS = ("--set-gtid-purged=OFF",)


class MysqlSource(DatabaseSource):
    engine = "mysql"

    def dump(self, ctx: BackupContext, container: str) -> dict[str, Any]:
        user = self.require_env(container, self.database.user_env_candidates)
        password = self.require_env(container, self.database.password_env_candidates)
        runner.redactor.add(password)
        # The password travels through drkit's environment to `docker exec -e MYSQL_PWD`:
        # it never appears on a command line, where any local user could read it.
        env = {PASSWORD_VAR: password}

        # MariaDB 11 images no longer ship the mysql* client names.
        mariadb = docker.has_executable(container, "mariadb-dump")
        client, dumper = ("mariadb", "mariadb-dump") if mariadb else ("mysql", "mysqldump")

        version = runner.run(
            docker.exec_args(
                container,
                [client, "-u", user, "--batch", "--skip-column-names", "-e", "SELECT VERSION()"],
                env_names=[PASSWORD_VAR],
            ),
            timeout=VERSION_TIMEOUT_S,
            env=env,
        ).stdout.strip()

        options = [*DUMP_OPTIONS] if mariadb else [*DUMP_OPTIONS, *MYSQL_ONLY_OPTIONS]
        dump = stream_dump(
            ctx,
            docker.exec_args(
                container, [dumper, "-u", user, *options, self.name], env_names=[PASSWORD_VAR]
            ),
            label=dumper,
            filename=self.filename("sql"),
            tags=self.tags(ctx),
            producer_env=env,
        )
        return {
            "engine_version": version,
            "snapshot": dump.snapshot_id,
            "bytes": dump.bytes_processed,
        }
