from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

import pytest

from drkit import docker
from drkit.config import Project, Volume
from drkit.errors import CommandError, DrkitError
from drkit.restic import BackupSummary, Restic
from drkit.sources import volume as volume_module
from drkit.sources.base import BackupContext, ItemResult
from drkit.sources.volume import VolumeSource

PROJECT = Project.model_validate(
    {
        "name": "shop",
        "path": "/srv/shop",
        "git": {"remote": "git@example.com:shop.git"},
        "compose": {"project_name": "shop-prod", "files": ["compose.yaml"]},
    }
)


class FakeDocker:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, mountpoint: Path) -> None:
        self.events: list[str] = []
        self.mountpoint = mountpoint
        self.fail_stop = False
        self.fail_start = False
        monkeypatch.setattr(docker, "compose_volume", self.compose_volume)
        monkeypatch.setattr(docker, "volume_mountpoint", lambda name: str(self.mountpoint))
        monkeypatch.setattr(docker, "volume_containers", lambda name: ["app", "worker"])
        monkeypatch.setattr(docker, "stop_containers", self.stop)
        monkeypatch.setattr(docker, "start_containers", self.start)

    def compose_volume(self, project: str, volume: str) -> str:
        self.events.append(f"resolve {project}/{volume}")
        return f"{project}_{volume}"

    def stop(self, containers: Sequence[str]) -> None:
        self.events.append(f"stop {','.join(containers)}")
        if self.fail_stop:
            raise CommandError("docker stop failed")

    def start(self, containers: Sequence[str]) -> None:
        self.events.append(f"start {','.join(containers)}")
        if self.fail_start:
            raise CommandError("docker start failed: no such network")


class FakeRestic:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.forgotten: list[str] = []

    def backup_paths(self, paths: Sequence[str], tags: Sequence[str]) -> BackupSummary:
        self.events.append(f"backup {paths[0]} {' '.join(tags)}")
        if self.fail:
            raise CommandError("restic could not read every file")
        return BackupSummary("snap1", 2048, 10)

    def forget(self, ids: Sequence[str]) -> int:
        self.forgotten += ids
        return len(ids)


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDocker:
    return FakeDocker(monkeypatch, tmp_path)


def backup(
    consistency: Literal["live", "stop"], restic: FakeRestic
) -> tuple[VolumeSource, ItemResult]:
    source = VolumeSource(PROJECT, Volume(name="uploads", consistency=consistency))
    ctx = BackupContext(restic=cast(Restic, restic), run_id="r1", host="prod-01")
    return source, source.backup(ctx)


def test_live_backup_of_the_mountpoint(fake_docker: FakeDocker) -> None:
    restic = FakeRestic(fake_docker.events)
    source, item = backup("live", restic)
    assert item.status == "ok"
    assert (item.snapshot, item.bytes) == ("snap1", 2048)
    assert fake_docker.events == [
        "resolve shop-prod/uploads",
        f"backup {fake_docker.mountpoint} drkit host:prod-01 run:r1 kind:volume "
        "project:shop volume:uploads",
    ]
    assert source.label == "shop/volume/uploads"


def test_stop_consistency_restarts_after_the_backup(fake_docker: FakeDocker) -> None:
    restic = FakeRestic(fake_docker.events)
    _, item = backup("stop", restic)
    assert item.status == "ok"
    assert [e.split(" ")[0] for e in fake_docker.events] == ["resolve", "stop", "backup", "start"]
    assert fake_docker.events[1] == "stop app,worker"


def test_services_restart_when_the_backup_fails(fake_docker: FakeDocker) -> None:
    restic = FakeRestic(fake_docker.events, fail=True)
    _, item = backup("stop", restic)
    assert item.status == "failed"
    assert item.error == "restic could not read every file"
    assert fake_docker.events[-1] == "start app,worker"


def test_services_restart_when_stopping_fails(fake_docker: FakeDocker) -> None:
    fake_docker.fail_stop = True
    restic = FakeRestic(fake_docker.events)
    _, item = backup("stop", restic)
    assert item.status == "failed"
    assert [e.split(" ")[0] for e in fake_docker.events] == ["resolve", "stop", "start"]


def test_failed_restart_fails_the_item_and_drops_its_snapshot(fake_docker: FakeDocker) -> None:
    fake_docker.fail_start = True
    restic = FakeRestic(fake_docker.events)
    _, item = backup("stop", restic)
    assert item.status == "failed"
    assert item.snapshot is None
    assert item.error == (
        "services stopped for the backup did not start again: docker start failed: no such network"
    )
    assert restic.forgotten == ["snap1"]


def test_unreadable_mountpoint_never_stops_services(fake_docker: FakeDocker) -> None:
    fake_docker.mountpoint = fake_docker.mountpoint / "missing"
    restic = FakeRestic(fake_docker.events)
    _, item = backup("stop", restic)
    assert item.status == "failed"
    assert (item.error or "").startswith("cannot read volume shop-prod_uploads at ")
    assert fake_docker.events == ["resolve shop-prod/uploads"]


def test_no_running_service_to_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(docker, "stop_containers", lambda c: calls.append("stop"))
    with volume_module.stopped([], label="x"):
        calls.append("body")
    assert calls == ["body"]


def test_restart_error_has_a_hint(fake_docker: FakeDocker) -> None:
    fake_docker.fail_start = True
    with pytest.raises(DrkitError) as excinfo, volume_module.stopped(["app"], label="x"):
        pass
    assert excinfo.value.hint is not None
    assert "docker compose up -d" in excinfo.value.hint


class TestExternalVolumes:
    @pytest.fixture
    def missing(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        def compose_volume(project: str, volume: str) -> str:
            raise DrkitError(f"volume {volume!r} of compose project {project!r} not found")

        directories: list[Path] = []

        def compose_config(project: str, files: Sequence[Path], directory: Path) -> object:
            directories.append(directory)
            return {"volumes": {"uploads": {"external": True, "name": "uploads"}}}

        monkeypatch.setattr(docker, "compose_volume", compose_volume)
        monkeypatch.setattr(docker, "compose_config", compose_config)
        return directories

    def test_backup_explains_external_volumes(self, missing: list[Path]) -> None:
        _, item = backup("live", FakeRestic([]))
        assert item.status == "failed"
        assert item.error == (
            "volume 'uploads' of project 'shop' is declared external in its compose files: "
            "drkit only backs up and restores volumes that Compose creates"
        )
        assert missing == [Path("/srv/shop")]

    def test_lookup_in_another_directory(self, missing: list[Path]) -> None:
        with pytest.raises(DrkitError, match="declared external"):
            volume_module.resolve(PROJECT, "uploads", Path("/restore/shop"))
        assert missing == [Path("/restore/shop")]

    def test_other_lookup_failures_are_kept(
        self, missing: list[Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_config(project: str, files: Sequence[Path], directory: Path) -> object:
            raise DrkitError("invalid compose configuration")

        monkeypatch.setattr(docker, "compose_config", no_config)
        with pytest.raises(DrkitError, match="volume 'uploads' of compose project 'shop-prod'"):
            volume_module.resolve(PROJECT, "uploads")
        monkeypatch.setattr(docker, "compose_config", lambda *args: {"volumes": {"uploads": {}}})
        with pytest.raises(DrkitError, match="not found"):
            volume_module.resolve(PROJECT, "uploads")
