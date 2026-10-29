import pytest

from drkit import runner
from drkit.config import Database, Project
from drkit.sources.base import BackupContext
from drkit.sources.mysql import MysqlSource
from tests.unit.sources.fakes import FakeHost, summary

PROJECT = Project.model_validate(
    {
        "name": "shop",
        "path": "/srv/shop",
        "git": {"remote": "git@example.com:shop.git"},
        "compose": {"files": ["compose.yaml"]},
    }
)
DB = Database(engine="mysql", service="mysql", database="legacy")
OPTIONS = ["--single-transaction", "--routines", "--triggers", "--events", "--no-tablespaces"]


@pytest.fixture(autouse=True)
def _container(host: FakeHost) -> None:
    host.env = {"MYSQL_USER": "app", "MYSQL_PASSWORD": "app-password-123"}
    host.run_outputs = {"mysql": "8.4.9\n", "mariadb": "11.4.8-MariaDB-ubu2404\n"}


def test_mysql_dump(ctx: BackupContext, host: FakeHost) -> None:
    host.pipes = [(0, 0, summary("snap", 64))]

    item = MysqlSource(PROJECT, DB).backup(ctx)

    assert item.status == "ok"
    assert (item.engine, item.engine_version, item.snapshot, item.bytes) == (
        "mysql",
        "8.4.9",
        "snap",
        64,
    )
    assert item.globals_snapshot is None
    assert host.containers == [("shop", "mysql")]
    producer, consumer, kwargs = host.pipe_calls[0]
    assert producer == [
        "docker", "exec", "-e", "MYSQL_PWD", "ctr", "mysqldump", "-u", "app", *OPTIONS, "legacy"
    ]  # fmt: skip
    assert consumer[4] == "shop/db/legacy.sql"
    assert kwargs["producer_env"] == {"MYSQL_PWD": "app-password-123"}
    assert host.run_calls[0] == [
        "docker", "exec", "-e", "MYSQL_PWD", "ctr",
        "mysql", "-u", "app", "--batch", "--skip-column-names", "-e", "SELECT VERSION()",
    ]  # fmt: skip
    assert host.run_envs[0] == {"MYSQL_PWD": "app-password-123"}


def test_password_never_on_command_line(ctx: BackupContext, host: FakeHost) -> None:
    host.pipes = [(0, 0, summary("snap", 1))]
    MysqlSource(PROJECT, DB).backup(ctx)
    argvs = [*host.run_calls, *(c[0] for c in host.pipe_calls), *(c[1] for c in host.pipe_calls)]
    assert all("app-password-123" not in arg for argv in argvs for arg in argv)
    assert runner.redact("app-password-123") == runner.REDACTED


def test_mariadb_client_names(ctx: BackupContext, host: FakeHost) -> None:
    host.executables = {"mariadb-dump"}
    host.pipes = [(0, 0, summary("snap", 1))]
    item = MysqlSource(PROJECT, DB).backup(ctx)
    assert item.engine_version == "11.4.8-MariaDB-ubu2404"
    assert host.run_calls[0][5] == "mariadb"
    assert host.pipe_calls[0][0][5] == "mariadb-dump"


def test_custom_credential_variables(ctx: BackupContext, host: FakeHost) -> None:
    host.env = {"DB_USER": "root", "DB_ROOT_PW": "root-password"}
    host.pipes = [(0, 0, summary("snap", 1))]
    db = Database(
        engine="mysql",
        service="mysql",
        database="legacy",
        user_env="DB_USER",
        password_env="DB_ROOT_PW",
    )
    assert MysqlSource(PROJECT, db).backup(ctx).status == "ok"
    assert host.pipe_calls[0][2]["producer_env"] == {"MYSQL_PWD": "root-password"}


def test_missing_password_variable(ctx: BackupContext, host: FakeHost) -> None:
    host.env = {"MYSQL_USER": "app"}
    item = MysqlSource(PROJECT, DB).backup(ctx)
    assert item.status == "failed"
    assert item.error == "environment variable MYSQL_PASSWORD is not set in the 'mysql' container"


def test_failed_dump(ctx: BackupContext, host: FakeHost) -> None:
    host.pipes = [(2, 0, summary("partial", 1), "mysqldump: Got error: 1049: Unknown database")]
    item = MysqlSource(PROJECT, DB).backup(ctx)
    assert item.status == "failed"
    assert (
        item.error
        == "mysqldump failed with exit code 2: mysqldump: Got error: 1049: Unknown database"
    )
    assert host.forgotten == ["partial"]
