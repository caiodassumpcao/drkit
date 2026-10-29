from collections.abc import Sequence
from typing import Any

import pytest

from drkit import docker, runner
from drkit.errors import DrkitError
from drkit.runner import CommandResult


def fake_run(
    monkeypatch: pytest.MonkeyPatch, code: int = 0, stdout: str = "", stderr: str = ""
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
        calls.append(list(args))
        return CommandResult(tuple(args), code, stdout, stderr, 0.0)

    monkeypatch.setattr(runner, "run", run)
    return calls


class TestComposeContainer:
    def test_filters_by_compose_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_run(monkeypatch, stdout="shop-db-1\tabc123\n")
        assert docker.compose_container("shop", "db") == "abc123"
        assert calls[0] == [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=shop",
            "--filter",
            "label=com.docker.compose.service=db",
            "--filter",
            "status=running",
            "--format",
            "{{.Names}}\t{{.ID}}",
        ]

    def test_first_replica_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_run(monkeypatch, stdout="shop-db-2\tbbb\nshop-db-1\taaa\n")
        assert docker.compose_container("shop", "db") == "aaa"

    def test_not_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_run(monkeypatch, stdout="\n")
        with pytest.raises(docker.ServiceNotRunningError, match="'db' of compose project 'shop'"):
            docker.compose_container("shop", "db")


class TestContainerEnv:
    def test_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_run(monkeypatch, stdout="app\n")
        assert docker.container_env("abc", "POSTGRES_USER") == "app"
        assert calls[0] == ["docker", "exec", "abc", "printenv", "POSTGRES_USER"]

    def test_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_run(monkeypatch, code=1)
        assert docker.container_env("abc", "X") is None

    def test_exec_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_run(monkeypatch, code=125, stderr="Error: No such container: abc")
        with pytest.raises(DrkitError, match="No such container"):
            docker.container_env("abc", "X")


def test_exec_args_forward_env_by_name() -> None:
    assert docker.exec_args("abc", ["mysqldump", "db"], env_names=["MYSQL_PWD"], stdin=True) == [
        "docker",
        "exec",
        "-i",
        "-e",
        "MYSQL_PWD",
        "abc",
        "mysqldump",
        "db",
    ]
    assert docker.exec_args("abc", ["true"]) == ["docker", "exec", "abc", "true"]


def test_has_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, code=127)
    assert docker.has_executable("abc", "mariadb-dump") is False
    assert calls[0] == ["docker", "exec", "abc", "mariadb-dump", "--version"]
    fake_run(monkeypatch)
    assert docker.has_executable("abc", "mysqldump") is True


def fake_runs(monkeypatch: pytest.MonkeyPatch, *stdouts: str) -> list[list[str]]:
    """Answer successive calls with ``stdouts`` (exit code 0)."""
    calls: list[list[str]] = []
    queue = list(stdouts)

    def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
        calls.append(list(args))
        return CommandResult(tuple(args), 0, queue.pop(0) if queue else "", "", 0.0)

    monkeypatch.setattr(runner, "run", run)
    return calls


class TestVolumes:
    def test_compose_volume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_run(monkeypatch, stdout="shop_uploads\n")
        assert docker.compose_volume("shop", "uploads") == "shop_uploads"
        assert calls[0][-4:] == [
            "--filter",
            "label=com.docker.compose.project=shop",
            "--filter",
            "label=com.docker.compose.volume=uploads",
        ]

    def test_compose_volume_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_run(monkeypatch, stdout="")
        with pytest.raises(DrkitError, match="volume 'uploads' of compose project 'shop'"):
            docker.compose_volume("shop", "uploads")

    def test_mountpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_run(monkeypatch, stdout="/var/lib/docker/volumes/shop_uploads/_data\n")
        assert docker.volume_mountpoint("shop_uploads") == (
            "/var/lib/docker/volumes/shop_uploads/_data"
        )

    def test_containers_using_a_volume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_run(monkeypatch, stdout="shop-worker-1\tbbb\nshop-app-1\taaa\n")
        assert docker.volume_containers("shop_uploads") == ["aaa", "bbb"]
        assert calls[0][2:4] == ["--filter", "volume=shop_uploads"]

    def test_stop_and_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_run(monkeypatch)
        docker.stop_containers(["aaa", "bbb"])
        docker.start_containers(["aaa", "bbb"])
        assert calls == [
            ["docker", "stop", "--time", "30", "aaa", "bbb"],
            ["docker", "start", "aaa", "bbb"],
        ]


class TestProjectImages:
    def test_pins_each_service_by_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_runs(
            monkeypatch,
            "c1\nc2\nc3\n",
            "db\tpostgres:16.4\tsha256:p\n"
            "app\tghcr.io/acme/app:1.2\tsha256:a\n"
            "app\tghcr.io/acme/app:1.2\tsha256:a\n",
            'sha256:a\t["ghcr.io/acme/app@sha256:aaa"]\nsha256:p\t["postgres@sha256:ppp"]\n',
        )
        assert docker.project_images("shop") == {
            "app": "ghcr.io/acme/app:1.2@sha256:aaa",
            "db": "postgres:16.4@sha256:ppp",
        }
        assert calls[2][-2:] == ["sha256:a", "sha256:p"]

    def test_local_build_keeps_its_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_runs(monkeypatch, "c1\n", "app\tshop-app\tsha256:x\n", "sha256:x\t[]\n")
        assert docker.project_images("shop") == {"app": "shop-app"}

    def test_no_running_containers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_runs(monkeypatch, "")
        assert docker.project_images("shop") == {}
        assert len(calls) == 1


@pytest.mark.parametrize(
    ("reference", "digests", "pinned"),
    [
        ("postgres:16", ["postgres@sha256:1"], "postgres:16@sha256:1"),
        ("postgres:16@sha256:old", ["postgres@sha256:1"], "postgres:16@sha256:1"),
        ("reg:5000/team/app", ["reg:5000/team/app@sha256:2"], "reg:5000/team/app@sha256:2"),
        ("m.io/app:1", ["x/app@sha256:3", "m.io/app@sha256:4"], "m.io/app:1@sha256:4"),
        ("app:1", ["retagged@sha256:5"], "app:1@sha256:5"),
        ("app:1", [], None),
    ],
)  # fmt: skip
def test_pin_reference(reference: str, digests: list[str], pinned: str | None) -> None:
    assert docker.pin_reference(reference, digests) == pinned
