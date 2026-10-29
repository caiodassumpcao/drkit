from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from drkit import runner, state, verify
from drkit.config import LoadedConfig, Manifest, Repository
from drkit.errors import ConfigError, DrkitError, ExitCode
from drkit.restic import Restic
from drkit.runmanifest import RunManifest
from drkit.runner import CommandResult, PipeResult
from drkit.sources.base import ItemResult

T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)

POSTGRES = ItemResult(
    kind="db",
    project="shop",
    name="shop",
    engine="postgres",
    engine_version="16.9 (Debian 16.9-1.pgdg120+1)",
    snapshot="dump1",
    globals_snapshot="roles1",
    status="ok",
)
MYSQL = ItemResult(
    kind="db",
    project="shop",
    name="legacy",
    engine="mysql",
    engine_version="8.4.9",
    snapshot="sql1",
    status="ok",
)
MARIADB = MYSQL.model_copy(
    update={"project": "blog", "name": "blog", "engine_version": "11.4.13-MariaDB-ubu2404"}
)


@dataclass
class FakeDocker:
    """Answers docker commands by the binary run inside the container."""

    outputs: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)
    pipe_codes: list[tuple[int, int]] = field(default_factory=list)
    runs: list[list[str]] = field(default_factory=list)
    pipes: list[tuple[list[str], list[str]]] = field(default_factory=list)

    def run(self, args: Sequence[str], **kwargs: Any) -> CommandResult:
        argv = list(args)
        self.runs.append(argv)
        key = argv[3] if argv[1] == "exec" else argv[1]
        queue = self.outputs.get(key)
        code, out, err = queue.pop(0) if queue else (0, "", "")
        if kwargs.get("check", True) and code != 0:
            raise DrkitError(f"{key} failed")
        return CommandResult(tuple(argv), code, out, err, 0.0)

    def pipe(self, producer: Sequence[str], consumer: Sequence[str], **kwargs: Any) -> PipeResult:
        self.pipes.append((list(producer), list(consumer)))
        p_code, c_code = self.pipe_codes.pop(0) if self.pipe_codes else (0, 0)
        return PipeResult(
            producer=CommandResult(tuple(producer), p_code, "", "dump error", 0.0),
            consumer=CommandResult(tuple(consumer), c_code, "", "ERROR: restore error", 0.0),
        )

    def execs(self, binary: str) -> list[list[str]]:
        return [a for a in self.runs if a[1] == "exec" and a[3] == binary]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    docker = FakeDocker()
    monkeypatch.setattr(runner, "run", docker.run)
    monkeypatch.setattr(runner, "pipe", docker.pipe)
    monkeypatch.setattr(verify, "sleep", lambda seconds: None)
    return docker


@pytest.fixture
def restic() -> Restic:
    repository = Repository(url="s3:https://s3.example.com/b", password_file=Path("/pw"))
    return Restic(repository, host="prod-01", env={"AWS_SECRET_ACCESS_KEY": "aws-secret"})


class TestPostgres:
    def test_restore_roles_then_dump_and_check(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {
            "pg_isready": [(2, "", ""), (0, "", "")],
            "psql": [(0, "3\n", ""), (0, "drkit-seed\n", "")],
        }

        check = verify.verify_database(restic, POSTGRES, "SELECT item FROM orders")

        assert check == verify.DatabaseCheck(
            label="shop/db/shop", engine="postgres", image="postgres:16", status="ok", tables=3
        )
        pull, run = fake.runs[0], fake.runs[1]
        assert pull == ["docker", "pull", "--quiet", "postgres:16"]
        container = run[run.index("--name") + 1]
        assert container.startswith("drkit-verify-")
        assert run[1:3] == ["run", "--detach"]
        assert run[run.index("--network") : run.index("--network") + 2] == ["--network", "none"]
        assert "drkit.verify=true" in run
        assert run[-3:] == ["-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16"]
        assert len(fake.execs("pg_isready")) == 2
        assert fake.pipes == [
            (
                ["restic", "dump", "roles1", "/shop/db/shop.globals.sql"],
                ["docker", "exec", "-i", container, "psql", "-U", "postgres", "-X", "-q",
                 "-d", "postgres"],
            ),
            (
                ["restic", "dump", "dump1", "/shop/db/shop.dump"],
                ["docker", "exec", "-i", container, "pg_restore", "-U", "postgres", "-d", "shop",
                 "--exit-on-error"],
            ),
        ]  # fmt: skip
        assert fake.execs("createdb")[0][-1] == "shop"
        assert fake.execs("psql")[1][-1] == "SELECT item FROM orders"
        assert fake.runs[-1] == ["docker", "rm", "--force", container]

    def test_no_tables(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {"psql": [(0, "0\n", "")]}
        check = verify.verify_database(restic, POSTGRES, None)
        assert check.status == "failed"
        assert check.error == "the restored database has no tables"
        assert fake.runs[-1][:3] == ["docker", "rm", "--force"]

    def test_failed_restore(self, fake: FakeDocker, restic: Restic) -> None:
        fake.pipe_codes = [(0, 0), (0, 1)]
        check = verify.verify_database(restic, POSTGRES, None)
        assert check.error == "pg_restore failed with exit code 1: ERROR: restore error"

    def test_failed_dump(self, fake: FakeDocker, restic: Restic) -> None:
        fake.pipe_codes = [(1, 0)]
        check = verify.verify_database(restic, POSTGRES, None)
        assert check.error == (
            "restic dump of shop/db/shop.globals.sql failed with exit code 1: dump error"
        )

    def test_failed_query(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {"psql": [(0, "1\n", ""), (1, "", 'ERROR:  relation "x" does not exist')]}
        check = verify.verify_database(restic, POSTGRES, "SELECT * FROM x")
        assert check.error == ('psql failed with exit code 1: ERROR:  relation "x" does not exist')


class TestMysql:
    def test_mysql(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {"mysql": [(0, "2\n", "")]}
        check = verify.verify_database(restic, MYSQL, None)
        assert (check.status, check.image, check.tables) == ("ok", "mysql:8.4", 2)
        run = fake.runs[1]
        assert run[-5:] == [
            "-e", "MYSQL_ALLOW_EMPTY_PASSWORD=yes", "-e", "MYSQL_DATABASE=legacy", "mysql:8.4"
        ]  # fmt: skip
        assert fake.execs("mysqladmin")[0][3:] == [
            "mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", "--silent"
        ]  # fmt: skip
        [(producer, consumer)] = fake.pipes
        assert producer == ["restic", "dump", "sql1", "/shop/db/legacy.sql"]
        assert consumer[-3:] == ["mysql", "-uroot", "legacy"]
        assert "table_schema = 'legacy'" in fake.execs("mysql")[0][-1]

    def test_mariadb(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {"mariadb": [(0, "1\n", "")]}
        check = verify.verify_database(restic, MARIADB, None)
        assert (check.status, check.image) == ("ok", "mariadb:11.4")
        assert "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=yes" in fake.runs[1]
        assert fake.execs("mariadb-admin")
        assert fake.pipes[0][1][-3:] == ["mariadb", "-uroot", "blog"]


class TestStartup:
    def test_container_exits(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {
            "pg_isready": [(1, "", "Error response from daemon: container x is not running")],
            "logs": [(0, "", "initdb: error: could not create directory")],
        }
        check = verify.verify_database(restic, POSTGRES, None)
        assert check.error == (
            "the verification database did not start: initdb: error: could not create directory"
        )

    def test_timeout(
        self, fake: FakeDocker, restic: Restic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verify, "READY_TIMEOUT_S", -1.0)
        fake.outputs = {"pg_isready": [(2, "", "")]}
        check = verify.verify_database(restic, POSTGRES, None)
        assert check.error == "the verification database did not start: no output"

    def test_failed_pull_still_cleans_up(self, fake: FakeDocker, restic: Restic) -> None:
        fake.outputs = {"pull": [(1, "", "manifest unknown")], "rm": [(1, "", "No such container")]}
        check = verify.verify_database(restic, POSTGRES, None)
        assert (check.status, check.image) == ("failed", "postgres:16")
        assert fake.runs[-1][:3] == ["docker", "rm", "--force"]

    def test_cleanup_errors_are_only_logged(
        self, fake: FakeDocker, restic: Restic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
            if args[1] == "rm":
                raise DrkitError("docker rm timed out")
            return fake.run(args, **kwargs)

        monkeypatch.setattr(runner, "run", run)
        fake.outputs = {"psql": [(0, "1\n", "")]}
        assert verify.verify_database(restic, POSTGRES, None).status == "ok"


@pytest.mark.parametrize(
    ("item", "image"),
    [
        (POSTGRES, "postgres:16"),
        (POSTGRES.model_copy(update={"engine_version": "13.16"}), "postgres:13"),
        (MYSQL.model_copy(update={"engine_version": "8.0.39"}), "mysql:8.0"),
        (MARIADB.model_copy(update={"engine_version": "10.11.9-MariaDB"}), "mariadb:10.11"),
    ],
)
def test_image_follows_the_server_version(item: ItemResult, image: str) -> None:
    engine = verify.Postgres() if item.engine == "postgres" else verify.Mysql(item.engine_version)
    assert engine.image(item.engine_version) == image


@pytest.mark.parametrize("version", [None, "", "unknown", "8"])
def test_unknown_version(version: str | None, fake: FakeDocker, restic: Restic) -> None:
    check = verify.verify_database(
        restic, MYSQL.model_copy(update={"engine_version": version}), None
    )
    assert check.status == "failed"
    assert (check.error or "").startswith("cannot tell the server version from")
    assert check.image is None


# --------------------------------------------------------------------------- run_verify

MANIFEST = Manifest.model_validate(
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
                "compose": {"files": ["compose.yaml"]},
                "databases": [
                    {
                        "engine": "postgres",
                        "service": "db",
                        "database": "shop",
                        "verify_query": "SELECT 1",
                    },
                ],
            }
        ],
        "notifications": {"on": ["failure"], "webhook": {"url": "https://hooks.test/x"}},
    }
)
RUN = RunManifest(
    run_id="20261029T030000Z-a1f3",
    host="prod-01",
    drkit_version="0.2.0",
    started_at=T0,
    finished_at=T0,
    status="partial",
    items=[
        POSTGRES,
        MYSQL.model_copy(update={"status": "failed", "snapshot": None, "error": "x"}),
        ItemResult(kind="volume", project="shop", name="uploads", snapshot="v1", status="ok"),
        MARIADB,
    ],
)


class FakeRepo(Restic):
    def __init__(self, *, check_error: str | None = None, ready: bool = True) -> None:
        super().__init__(MANIFEST.repository, host="prod-01", env={})
        self.check_error = check_error
        self.ready = ready
        self.checked: list[str | None] = []

    def ensure_ready(self) -> None:
        if not self.ready:
            raise ConfigError("restic repository is not initialized")

    def check(self, read_data_subset: str | None = None) -> None:
        self.checked.append(read_data_subset)
        if self.check_error:
            raise DrkitError(self.check_error)


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []

    def verify_database(
        restic: Restic, item: ItemResult, query: str | None
    ) -> verify.DatabaseCheck:
        calls.append((item.label, query))
        failed = item.engine_version is not None and "MariaDB" in item.engine_version
        return verify.DatabaseCheck(
            label=item.label,
            engine="x",
            image="i",
            status="failed" if failed else "ok",
            error="boom" if failed else None,
        )

    monkeypatch.setattr(verify, "verify_database", verify_database)
    monkeypatch.setattr(verify, "load_run", lambda restic, run_id: RUN)
    return calls


def run(tmp_path: Path, repo: FakeRepo, messages: list[str], **kwargs: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        messages.append(request.read().decode())
        return httpx.Response(200)

    return verify.run_verify(
        LoadedConfig(path=tmp_path / "dr.yaml", manifest=MANIFEST),
        restic=repo,
        state_directory=tmp_path / "state",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


class TestRunVerify:
    def test_checks_repository_and_every_backed_up_database(
        self, tmp_path: Path, verified: list[tuple[str, str | None]]
    ) -> None:
        repo = FakeRepo()
        messages: list[str] = []

        report = run(tmp_path, repo, messages)

        assert repo.checked == ["5%"]
        assert verified == [("shop/db/shop", "SELECT 1"), ("blog/db/blog", None)]
        assert not report.ok
        assert report.exit_code is ExitCode.PARTIAL_FAILURE
        last = state.load(tmp_path / "state").last_verify
        assert last is not None
        assert (last.run_id, last.status) == (RUN.run_id, "failed")
        [message] = messages
        assert "drkit verify on prod-01: FAILED" in message
        assert "1/2 database(s) restored" in message
        assert "blog/db/blog: boom" in message

    def test_repository_errors_do_not_stop_restore_tests(
        self, tmp_path: Path, verified: list[tuple[str, str | None]]
    ) -> None:
        report = run(
            tmp_path, FakeRepo(check_error="pack abc is damaged"), [], read_data_subset=None
        )
        assert report.repository_error == "pack abc is damaged"
        assert len(report.databases) == 2
        message = verify.format_message("h", report)
        assert "repository check FAILED" in message
        assert "✗ repository: pack abc is damaged" in message

    def test_success(
        self, tmp_path: Path, verified: list[tuple[str, str | None]], monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(
            verify, "load_run", lambda restic, run_id: RUN.model_copy(update={"items": [POSTGRES]})
        )
        messages: list[str] = []
        report = run(tmp_path, FakeRepo(), messages)
        assert report.ok
        assert report.exit_code is ExitCode.OK
        assert messages == []  # notifications.on: [failure]
        assert verify.format_message("h", report).splitlines()[0] == "drkit verify on h: OK"

    def test_state_failure_is_reported(
        self, tmp_path: Path, verified: list[tuple[str, str | None]]
    ) -> None:
        (tmp_path / "state").write_text("not a directory")
        report = run(tmp_path, FakeRepo(), [])
        assert report.errors[0].startswith("state: cannot ")

    def test_cannot_start(self, tmp_path: Path, verified: list[tuple[str, str | None]]) -> None:
        messages: list[str] = []
        with pytest.raises(ConfigError):
            run(tmp_path, FakeRepo(ready=False), messages)
        assert "restic repository is not initialized" in messages[0]
        assert verified == []
