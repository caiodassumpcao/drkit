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
