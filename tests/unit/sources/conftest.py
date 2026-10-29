from collections.abc import Iterator
from pathlib import Path

import pytest

from drkit import docker, runner
from drkit.config import Repository
from drkit.restic import Restic
from drkit.sources.base import BackupContext
from tests.unit.sources.fakes import FakeHost


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHost]:
    fake = FakeHost()
    monkeypatch.setattr(docker, "compose_container", fake.compose_container)
    monkeypatch.setattr(docker, "container_env", fake.container_env)
    monkeypatch.setattr(docker, "has_executable", fake.has_executable)
    monkeypatch.setattr(runner, "run", fake.run)
    monkeypatch.setattr(runner, "pipe", fake.pipe)
    yield fake
    runner.redactor.clear()


@pytest.fixture
def ctx() -> BackupContext:
    repository = Repository(url="s3:https://s3.example.com/b", password_file=Path("/pw"))
    restic = Restic(repository, host="prod-01", env={})
    return BackupContext(restic=restic, run_id="20261029T030000Z-a1f3", host="prod-01")
