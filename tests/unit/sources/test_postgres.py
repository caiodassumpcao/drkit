import pytest

from drkit import docker, runner
from drkit.config import Database, Project
from drkit.errors import CommandError
from drkit.sources import base
from drkit.sources.base import BackupContext, DumpFailedError
from drkit.sources.postgres import PostgresSource
from tests.unit.sources.fakes import FakeHost, summary

PROJECT = Project.model_validate(
    {
        "name": "shop",
        "path": "/srv/shop",
        "git": {"remote": "git@example.com:shop.git"},
        "compose": {"project_name": "shop-prod", "files": ["compose.yaml"]},
    }
)
DB = Database(engine="postgres", service="db", database="shop")


def test_tags(ctx: BackupContext) -> None:
    assert PostgresSource(PROJECT, DB).tags(ctx) == [
        "drkit",
        "host:prod-01",
        "run:20261029T030000Z-a1f3",
        "kind:db",
        "project:shop",
        "db:shop",
    ]


class TestStreamDump:
    def test_success(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(0, 0, summary("snap1", 10))]
        result = base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=["t"])
        assert result.snapshot_id == "snap1"
        producer, consumer, kwargs = host.pipe_calls[0]
        assert producer == ["dump"]
        assert consumer[:5] == ["restic", "backup", "--stdin", "--stdin-filename", "f"]
        assert kwargs["consumer_env"] == ctx.restic.env
        assert host.forgotten == []

    def test_failed_dump_forgets_the_partial_snapshot(
        self, ctx: BackupContext, host: FakeHost
    ) -> None:
        host.pipes = [(1, 0, summary("partial", 3), "pg_dump: error: connection failed")]
        with pytest.raises(DumpFailedError, match="dump failed with exit code 1: pg_dump: error"):
            base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=[])
        assert host.forgotten == ["partial"]

    def test_empty_stream_snapshot_is_forgotten_despite_restic_exit_3(
        self, ctx: BackupContext, host: FakeHost
    ) -> None:
        # Real restic output when the dump dies before writing anything.
        host.pipes = [(1, 3, summary("empty", 0), "pg_dump: error", "no data read")]
        with pytest.raises(DumpFailedError, match="dump failed with exit code 1"):
            base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=[])
        assert host.forgotten == ["empty"]

    def test_restic_partial_read_is_a_failure(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(0, 3, summary("partial", 1), "", "could not be read")]
        with pytest.raises(CommandError, match="restic backup of f failed with exit code 3"):
            base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=[])
        assert host.forgotten == ["partial"]

    def test_failed_dump_without_snapshot(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(2, 1, "", "")]
        with pytest.raises(DumpFailedError, match=r"^dump failed with exit code 2$"):
            base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=[])
        assert host.forgotten == []

    def test_restic_failure(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(0, 1, "", "", "Fatal: unable to save snapshot")]
        with pytest.raises(CommandError, match="restic backup of f failed with exit code 1: Fatal"):
            base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=[])

    def test_restic_without_summary(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(0, 0, "garbage")]
        with pytest.raises(CommandError, match="restic backup of f did not report a snapshot"):
            base.stream_dump(ctx, ["dump"], label="dump", filename="f", tags=[])


class TestPostgresSource:
    @pytest.fixture(autouse=True)
    def _container(self, host: FakeHost) -> None:
        host.env = {"POSTGRES_USER": "app"}
        host.run_outputs = {"psql": "16.9 (Debian 16.9-1.pgdg120+1)\n"}

    def test_dumps_database_and_globals(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(0, 0, summary("dump", 100)), (0, 0, summary("roles", 5))]

        item = PostgresSource(PROJECT, DB).backup(ctx)

        assert item.status == "ok"
        assert item.model_dump(exclude={"duration_s"}) == {
            "kind": "db",
            "project": "shop",
            "name": "shop",
            "engine": "postgres",
            "engine_version": "16.9",
            "snapshot": "dump",
            "globals_snapshot": "roles",
            "bytes": 105,
            "status": "ok",
            "error": None,
        }
        assert host.containers == [("shop-prod", "db")]
        dump, roles = (call[0] for call in host.pipe_calls)
        assert dump == ["docker", "exec", "ctr", "pg_dump", "-U", "app", "-Fc", "shop"]
        assert roles == ["docker", "exec", "ctr", "pg_dumpall", "-U", "app", "--globals-only"]
        assert host.pipe_calls[0][1][4] == "shop/db/shop.dump"
        assert host.pipe_calls[1][1][4] == "shop/db/shop.globals.sql"
        assert host.run_calls[0][-6:] == [
            "-U",
            "app",
            "-d",
            "postgres",
            "-XtAc",
            "SHOW server_version",
        ]

    def test_custom_user_env(self, ctx: BackupContext, host: FakeHost) -> None:
        host.env = {"DB_OWNER": "owner"}
        host.pipes = [(0, 0, summary("d", 1)), (0, 0, summary("g", 1))]
        db = Database(engine="postgres", service="db", database="shop", user_env="DB_OWNER")
        assert PostgresSource(PROJECT, db).backup(ctx).status == "ok"
        assert host.pipe_calls[0][0][5] == "owner"

    def test_failed_dump_is_a_failed_item(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(1, 0, summary("partial", 1), 'pg_dump: database "shop" does not exist')]
        item = PostgresSource(PROJECT, DB).backup(ctx)
        assert item.status == "failed"
        assert item.snapshot is None
        assert (
            item.error == 'pg_dump failed with exit code 1: pg_dump: database "shop" does not exist'
        )
        assert host.forgotten == ["partial"]
        assert len(host.pipe_calls) == 1  # globals are not attempted

    def test_failed_globals_discard_the_dump(self, ctx: BackupContext, host: FakeHost) -> None:
        host.pipes = [(0, 0, summary("dump", 1)), (1, 0, "", "permission denied")]
        item = PostgresSource(PROJECT, DB).backup(ctx)
        assert item.status == "failed"
        assert item.error is not None
        assert item.error.startswith("pg_dumpall --globals-only failed")
        assert host.forgotten == ["dump"]

    def test_missing_user_variable(self, ctx: BackupContext, host: FakeHost) -> None:
        host.env = {}
        item = PostgresSource(PROJECT, DB).backup(ctx)
        assert item.status == "failed"
        assert item.error == "environment variable POSTGRES_USER is not set in the 'db' container"
        assert host.pipe_calls == []

    def test_service_not_running(self, ctx: BackupContext, monkeypatch: pytest.MonkeyPatch) -> None:
        def not_running(project: str, service: str) -> str:
            raise docker.ServiceNotRunningError("service 'db' has no running container")

        monkeypatch.setattr(docker, "compose_container", not_running)
        item = PostgresSource(PROJECT, DB).backup(ctx)
        assert item.status == "failed"
        assert item.error == "service 'db' has no running container"

    def test_errors_are_redacted(self, ctx: BackupContext, host: FakeHost) -> None:
        runner.redactor.add("s3cr3t-password")
        host.pipes = [(1, 0, "", "auth failed for s3cr3t-password")]
        item = PostgresSource(PROJECT, DB).backup(ctx)
        assert item.error is not None
        assert "s3cr3t-password" not in item.error
