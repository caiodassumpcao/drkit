import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from drkit import docker, runner
from drkit.config import LoadedConfig, Manifest, Repository
from drkit.errors import ConfigError, DrkitError
from drkit.restic import Restic
from drkit.restore import database as database_module
from drkit.restore import steps
from drkit.runmanifest import RunManifest
from drkit.runner import CommandResult, PipeResult
from drkit.sources.base import ItemResult
from tests.unit.sources.fakes import summary

RUN_ID = "20261029T030000Z-a1f3"
T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)

POSTGRES = ItemResult(
    kind="db",
    project="shop",
    name="shop",
    engine="postgres",
    engine_version="16.9",
    snapshot="dump1" * 4,
    globals_snapshot="roles1" * 4,
    status="ok",
)
MYSQL = ItemResult(
    kind="db",
    project="shop",
    name="legacy",
    engine="mysql",
    engine_version="8.4.9",
    snapshot="sql1" * 4,
    status="ok",
)


@dataclass
class FakeServer:
    """Database containers and restic, answering by the binary that runs."""

    env: dict[str, str] = field(
        default_factory=lambda: {
            "POSTGRES_USER": "app",
            "MYSQL_USER": "app",
            "MYSQL_PASSWORD": "mysql-secret",
        }
    )
    executables: set[str] = field(default_factory=set)
    outputs: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    pipe_codes: list[tuple[int, int]] = field(default_factory=list)
    runs: list[list[str]] = field(default_factory=list)
    envs: list[Any] = field(default_factory=list)
    pipes: list[tuple[list[str], list[str], dict[str, Any]]] = field(default_factory=list)
    services: list[tuple[str, str]] = field(default_factory=list)

    def compose_container(self, project: str, service: str) -> str:
        self.services.append((project, service))
        return "ctr"

    def run(self, args: Sequence[str], **kwargs: Any) -> CommandResult:
        argv = list(args)
        self.runs.append(argv)
        self.envs.append(kwargs.get("env"))
        if argv[:2] == ["restic", "snapshots"]:
            data = [{"id": argv[-1], "time": "2026-10-29T03:00:00Z"}]
            return CommandResult(tuple(argv), 0, json.dumps(data), "", 0.0)
        key = argv[argv.index("ctr") + 1] if "ctr" in argv else argv[1]
        queue = self.outputs.get(key)
        code, out = queue.pop(0) if queue else (0, self.defaults(argv, key))
        return CommandResult(tuple(argv), code, out, "ERROR: failed" if code else "", 0.0)

    def defaults(self, argv: list[str], key: str) -> str:
        if key == "printenv":
            return self.env.get(argv[-1], "")
        if key in ("psql", "mysql", "mariadb") and "count(*)" in argv[-1]:
            return "3\n"
        if key == "psql" and "pg_database" in argv[-1]:
            return "1\n"
        if key == "--version":
            return ""
        return "16.9 (Debian)\n" if key == "psql" else "8.4.9\n"

    def pipe(self, producer: Sequence[str], consumer: Sequence[str], **kwargs: Any) -> PipeResult:
        self.pipes.append((list(producer), list(consumer), kwargs))
        p_code, c_code = self.pipe_codes.pop(0) if self.pipe_codes else (0, 0)
        backup = consumer[:2] == ["restic", "backup"]
        out = summary(f"safety{len(self.pipes)}", 10) if backup else ""
        return PipeResult(
            producer=CommandResult(tuple(producer), p_code, "", "dump error", 0.0),
            consumer=CommandResult(tuple(consumer), c_code, out, "ERROR: restore error", 0.0),
        )

    def execs(self, binary: str) -> list[list[str]]:
        return [a for a in self.runs if a[:2] == ["docker", "exec"] and binary in a]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeServer]:
    fake = FakeServer()
    monkeypatch.setattr(docker, "compose_container", fake.compose_container)
    monkeypatch.setattr(
        docker,
        "container_env",
        lambda container, name: fake.env.get(name),
    )
    monkeypatch.setattr(docker, "has_executable", lambda container, name: name in fake.executables)
    monkeypatch.setattr(docker, "sleep", lambda seconds: None)
    monkeypatch.setattr(runner, "run", fake.run)
    monkeypatch.setattr(runner, "pipe", fake.pipe)
    yield fake
    runner.redactor.clear()


def loaded(**database: Any) -> LoadedConfig:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "host": "prod-01",
            "repository": {"url": "s3:https://s3.example.com/b", "password_file": "/pw"},
            "retention": {"daily": 7},
            "projects": [
                {
                    "name": "shop",
                    "path": "/srv/shop",
                    "git": {"remote": "git@example.com:shop.git"},
                    "compose": {"project_name": "shop-prod", "files": ["compose.yaml"]},
                    "databases": [
                        {"engine": "postgres", "service": "db", "database": "shop", **database},
                        {"engine": "mysql", "service": "mysql", "database": "legacy"},
                    ],
                }
            ],
        }
    )
    return LoadedConfig(path=Path("/etc/drkit/dr.yaml"), manifest=manifest)


@pytest.fixture
def restic() -> Restic:
    repository = Repository(url="s3:https://s3.example.com/b", password_file=Path("/pw"))
    return Restic(repository, host="prod-01", env={})


def run_with(*items: ItemResult, status: str = "ok") -> RunManifest:
    return RunManifest(
        run_id=RUN_ID,
        host="prod-01",
        drkit_version="0.3.0",
        started_at=T0,
        finished_at=T0,
        status=status,  # type: ignore[arg-type]
        items=list(items),
    )


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> list[RunManifest]:
    """The run find_run returns; tests replace its content."""
    runs = [run_with(POSTGRES, MYSQL)]
    lookups: list[str] = []

    def find_run(restic: Restic, run_id: str, *, accept: Any, what: str) -> RunManifest:
        lookups.append(run_id)
        if run_id == "latest" and not accept(runs[0]):
            raise DrkitError(f"no successful run contains {what}")
        return runs[0]

    monkeypatch.setattr(database_module, "find_run", find_run)
    return runs


def test_plan_lists_the_steps(server: FakeServer, restic: Restic, run: list[RunManifest]) -> None:
    plan = database_module.plan(loaded(), "shop", restic, database="shop")

    assert (plan.kind, plan.target, plan.run_id) == ("db", "shop/db/shop", RUN_ID)
    assert [step.title for step in plan.steps] == [
        "Locate the dumps in the run",
        "Safety dump of the current databases",
        "Restore the dumps",
        "Sanity check",
    ]
    assert plan.steps[0].plan == ("shop/db/shop: snapshot dump1dum (postgres 16.9) → service db",)
    assert plan.steps[1].plan == ("shop/db/shop: kind:pre-restore snapshot",)
    assert plan.steps[2].plan == (
        "shop/db/shop: pg_restore --clean --if-exists --single-transaction",
    )
    assert plan.warnings == []
    assert server.runs == [], "planning changes nothing"


def test_postgres_restore(server: FakeServer, restic: Restic, run: list[RunManifest]) -> None:
    plan = database_module.plan(loaded(), "shop", restic, database="shop")

    report = steps.execute(plan)

    assert report.ok, report.steps
    assert [s.detail for s in report.steps] == [
        "shop/db/shop: dump found, service db runs 16.9",
        "shop/db/shop: snapshot safety1",
        "shop/db/shop into service db",
        "shop/db/shop: 3 table(s)",
    ]
    # Both snapshots of the item are checked before anything changes.
    assert ["restic", "snapshots", "--json", POSTGRES.snapshot] in server.runs
    assert ["restic", "snapshots", "--json", POSTGRES.globals_snapshot] in server.runs
    safety, roles_safety, restore = server.pipes
    assert safety[0][-5:] == ["pg_dump", "-U", "app", "-Fc", "shop"]
    tags = [safety[1][i + 1] for i, arg in enumerate(safety[1]) if arg == "--tag"]
    assert tags == [
        "drkit",
        "host:prod-01",
        "kind:pre-restore",
        f"restore:{plan.restore_id}",
        "item:db",
        "project:shop",
        "db:shop",
    ]
    assert roles_safety[0][-4:] == ["pg_dumpall", "-U", "app", "--globals-only"]
    assert restore[0] == ["restic", "dump", POSTGRES.snapshot, "/shop/db/shop.dump"]
    assert restore[1] == [
        "docker",
        "exec",
        "-i",
        "ctr",
        "pg_restore",
        "-U",
        "app",
        "-d",
        "shop",
        "--clean",
        "--if-exists",
        "--single-transaction",
    ]
    # restore db leaves roles alone: only the database itself goes back in time.
    assert not [p for p in server.pipes if "globals.sql" in " ".join(p[0])]
    probe = ["pg_isready", "-h", "127.0.0.1", "-U", "app", "-d", "postgres", "-q"]
    assert ["docker", "exec", "ctr", *probe] in server.runs


def test_missing_postgres_database_is_created(
    server: FakeServer, restic: Restic, run: list[RunManifest]
) -> None:
    server.outputs["psql"] = [(0, "")]  # no such database yet
    target = database_module.database_targets(loaded().manifest.projects[0], run[0], only="shop")[0]
    database_module.restore_database(restic, target, roles=True)
    assert server.execs("createdb") == [["docker", "exec", "ctr", "createdb", "-U", "app", "shop"]]
    roles = server.pipes[0]
    assert roles[0] == ["restic", "dump", POSTGRES.globals_snapshot, "/shop/db/shop.globals.sql"]
    psql = ["psql", "-U", "app", "-X", "-q", "-d", "postgres"]
    assert roles[1] == ["docker", "exec", "-i", "ctr", *psql]


def test_mysql_restore_keeps_the_password_off_the_command_line(
    server: FakeServer, restic: Restic, run: list[RunManifest]
) -> None:
    server.executables = {"mariadb"}
    plan = database_module.plan(loaded(), "shop", restic, database="legacy", safety_backup=False)

    report = steps.execute(plan)

    assert report.ok, report.steps
    assert report.steps[1].detail == "not taken (--no-safety-backup)"
    assert plan.warnings == [
        "no safety dump: the current data cannot be recovered after the restore"
    ]
    [restore] = server.pipes
    assert restore[1] == [
        "docker",
        "exec",
        "-i",
        "-e",
        "MYSQL_PWD",
        "ctr",
        "mariadb",
        "-u",
        "app",
        "legacy",
    ]
    assert restore[2]["consumer_env"] == {"MYSQL_PWD": "mysql-secret"}
    mariadb = ["docker", "exec", "-e", "MYSQL_PWD", "ctr", "mariadb", "-u", "app"]
    assert [*mariadb, "-e", "CREATE DATABASE IF NOT EXISTS `legacy`"] in server.runs
    assert all("mysql-secret" not in " ".join(argv) for argv in server.runs)
    assert all("mysql-secret" not in " ".join(p[0] + p[1]) for p in server.pipes)
    probe = ["mariadb-admin", "ping", "-h", "127.0.0.1", "-u", "app", "--silent"]
    assert ["docker", "exec", "-e", "MYSQL_PWD", "ctr", *probe] in server.runs


def test_every_database_of_the_project_by_default(
    server: FakeServer, restic: Restic, run: list[RunManifest]
) -> None:
    plan = database_module.plan(loaded(), "shop", restic)
    assert plan.target == "shop/db/shop, shop/db/legacy"
    report = steps.execute(plan)
    assert report.ok
    assert report.steps[3].detail == "shop/db/shop: 3 table(s); shop/db/legacy: 3 table(s)"


def test_into_another_service(server: FakeServer, restic: Restic, run: list[RunManifest]) -> None:
    plan = database_module.plan(loaded(), "shop", restic, database="shop", into="db-staging")
    assert plan.steps[0].plan[0].endswith("→ service db-staging")
    steps.execute(plan)
    assert set(server.services) == {("shop-prod", "db-staging")}


def test_verify_query_runs_in_the_sanity_check(
    server: FakeServer, restic: Restic, run: list[RunManifest]
) -> None:
    plan = database_module.plan(
        loaded(verify_query="SELECT count(*) FROM orders"), "shop", restic, database="shop"
    )
    assert plan.steps[3].plan == ("shop/db/shop: count tables, run verify_query",)
    report = steps.execute(plan, from_step=4)
    assert report.steps[3].detail == "shop/db/shop: 3 table(s), verify_query ok"


def test_empty_database_fails_the_sanity_check(
    server: FakeServer, restic: Restic, run: list[RunManifest]
) -> None:
    server.outputs["psql"] = [(0, "0\n")]
    plan = database_module.plan(loaded(), "shop", restic, database="shop")
    report = steps.execute(plan, from_step=4)
    assert report.steps[3].error == "shop/db/shop: the restored database has no tables"


def test_failed_restore_is_reported(
    server: FakeServer, restic: Restic, run: list[RunManifest]
) -> None:
    server.pipe_codes = [(0, 0), (0, 0), (0, 1)]
    plan = database_module.plan(loaded(), "shop", restic, database="shop")
    report = steps.execute(plan)
    assert [s.status for s in report.steps] == ["ok", "ok", "failed", "not_run"]
    assert report.steps[2].error == "pg_restore failed with exit code 1: ERROR: restore error"


class TestTargets:
    def test_unknown_database(self, server: FakeServer, restic: Restic) -> None:
        with pytest.raises(ConfigError, match="project 'shop' has no database 'nope'") as exc:
            database_module.plan(loaded(), "shop", restic, database="nope")
        assert exc.value.hint == "databases of shop: shop, legacy"

    def test_no_successful_run(
        self, server: FakeServer, restic: Restic, run: list[RunManifest]
    ) -> None:
        run[0] = run_with(MYSQL)
        with pytest.raises(DrkitError, match="no successful run contains database 'shop'"):
            database_module.plan(loaded(), "shop", restic, database="shop")

    def test_explicit_run_without_the_database(
        self, server: FakeServer, restic: Restic, run: list[RunManifest]
    ) -> None:
        run[0] = run_with(MYSQL)
        with pytest.raises(DrkitError, match=f"run {RUN_ID} has no backup of database 'shop'"):
            database_module.plan(loaded(), "shop", restic, run_id=RUN_ID, database="shop")
        run[0] = run_with()
        with pytest.raises(DrkitError, match=f"run {RUN_ID} has no database backup of project"):
            database_module.plan(loaded(), "shop", restic, run_id=RUN_ID)

    def test_failed_item(self, server: FakeServer, restic: Restic, run: list[RunManifest]) -> None:
        failed = POSTGRES.model_copy(update={"status": "failed", "snapshot": None})
        run[0] = run_with(failed, status="partial")
        with pytest.raises(DrkitError, match=r"shop/db/shop failed in run .* cannot be restored"):
            database_module.plan(loaded(), "shop", restic, run_id=RUN_ID, database="shop")

    def test_database_no_longer_in_the_manifest(
        self, server: FakeServer, restic: Restic, run: list[RunManifest]
    ) -> None:
        run[0] = run_with(POSTGRES.model_copy(update={"name": "old"}))
        with pytest.raises(ConfigError, match=r"shop/db/old \(postgres\) of run .* not in the"):
            database_module.plan(loaded(), "shop", restic, run_id=RUN_ID)


def test_safety_dumps_belong_to_no_run(restic: Restic) -> None:
    ctx = database_module.SafetyContext(restic=restic, run_id="restore1", host="prod-01")
    assert ctx.tags("db", project="shop", db="orders") == [
        "drkit",
        "host:prod-01",
        "kind:pre-restore",
        "restore:restore1",
        "item:db",
        "project:shop",
        "db:orders",
    ]


class TestServerVersion:
    @pytest.mark.parametrize(
        ("item", "version", "outcome"),
        [
            (POSTGRES, "16.14 (Debian)", "16.14"),
            (POSTGRES, "17.2", "17.2"),
            (POSTGRES, "15.8", "newer than the 15.8 of service 'db'"),
            (MYSQL, "8.4.11", "8.4.11"),
            (MYSQL, "8.0.40", "newer than the 8.0.40 of service 'mysql'"),
            (MYSQL, "11.4.13-MariaDB-ubu2404", "a different server family"),
            (MYSQL, "unknown", "unknown"),
        ],
    )
    def test_target_must_not_be_older(
        self,
        server: FakeServer,
        restic: Restic,
        run: list[RunManifest],
        item: ItemResult,
        version: str,
        outcome: str,
    ) -> None:
        key = "psql" if item.engine == "postgres" else "mysql"
        server.outputs[key] = [(0, version + "\n")]
        plan = database_module.plan(loaded(), "shop", restic, database=item.name)
        report = steps.execute(plan)
        step = report.steps[0]
        if step.status == "ok":
            assert step.detail is not None
            assert step.detail.endswith(f"runs {outcome}")
        else:
            assert step.error is not None
            assert outcome in step.error
            assert report.steps[1].status == "not_run", "nothing changes on a version mismatch"
