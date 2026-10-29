"""PostgreSQL: ``pg_dump -Fc`` plus cluster globals (roles), streamed into restic."""

from __future__ import annotations

from typing import Any

from drkit import docker, runner
from drkit.errors import DrkitError
from drkit.sources.base import BackupContext, DatabaseSource, stream_dump

VERSION_TIMEOUT_S = 30.0


class PostgresSource(DatabaseSource):
    engine = "postgres"

    def dump(self, ctx: BackupContext, container: str) -> dict[str, Any]:
        # Inside the official image, local socket connections are trusted: no password needed.
        user = self.require_env(container, self.database.effective_user_env)
        version = self.server_version(container, user)
        tags = self.tags(ctx)

        dump = stream_dump(
            ctx,
            docker.exec_args(container, ["pg_dump", "-U", user, "-Fc", self.name]),
            label="pg_dump",
            filename=self.filename("dump"),
            tags=tags,
        )
        try:
            # Roles and their passwords live outside any database: without them a restored
            # dump has owners and grants pointing at roles that do not exist.
            roles = stream_dump(
                ctx,
                docker.exec_args(container, ["pg_dumpall", "-U", user, "--globals-only"]),
                label="pg_dumpall --globals-only",
                filename=self.filename("globals.sql"),
                tags=tags,
            )
        except DrkitError:
            ctx.restic.forget([dump.snapshot_id])
            raise
        return {
            "engine_version": version,
            "snapshot": dump.snapshot_id,
            "globals_snapshot": roles.snapshot_id,
            "bytes": dump.bytes_processed + roles.bytes_processed,
        }

    def server_version(self, container: str, user: str) -> str:
        # Connect to the maintenance database: the target may not exist, and that failure
        # belongs to pg_dump.
        result = runner.run(
            docker.exec_args(
                container, ["psql", "-U", user, "-d", "postgres", "-XtAc", "SHOW server_version"]
            ),
            timeout=VERSION_TIMEOUT_S,
        )
        # e.g. "16.9 (Debian 16.9-1.pgdg120+1)"
        return result.stdout.strip().split(" ", 1)[0]
