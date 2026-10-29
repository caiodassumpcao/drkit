import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from drkit import runner
from drkit.config import SystemPath
from drkit.errors import MissingDependencyError
from drkit.restic import BackupSummary, Restic
from drkit.runner import CommandResult
from drkit.sources import system
from drkit.sources.base import BackupContext
from drkit.sources.paths import SystemPathsSource
from drkit.sources.system import SystemInventorySource


class FakeRestic:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.documents: dict[str, str] = {}
        self.tags: list[str] = []

    def backup_paths(self, paths: Sequence[str], tags: Sequence[str]) -> BackupSummary:
        self.paths = list(paths)
        self.tags = list(tags)
        return BackupSummary("paths1", 10, 10)

    def backup_bytes(self, data: str, filename: str, tags: Sequence[str]) -> BackupSummary:
        self.documents[filename] = data
        self.tags = list(tags)
        return BackupSummary("inv1", len(data), 1)


def ctx(restic: FakeRestic) -> BackupContext:
    return BackupContext(restic=cast(Restic, restic), run_id="r1", host="prod-01")


class TestPaths:
    def test_whole_paths_and_include_globs(self, tmp_path: Path) -> None:
        nginx = tmp_path / "nginx"
        units = tmp_path / "systemd"
        nginx.mkdir()
        units.mkdir()
        for name in ("app-web.service", "app-web.timer", "other.service"):
            (units / name).write_text("")
        restic = FakeRestic()
        source = SystemPathsSource(
            [
                SystemPath(path=nginx),
                SystemPath(path=units, include=["app-*.service", "app-*.timer"]),
            ]
        )

        item = source.backup(ctx(restic))

        assert (item.status, item.snapshot, item.label) == ("ok", "paths1", "system/paths")
        assert restic.paths == [
            str(nginx),
            str(units / "app-web.service"),
            str(units / "app-web.timer"),
        ]
        assert restic.tags == ["drkit", "host:prod-01", "run:r1", "kind:system"]

    def test_include_without_match_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        source = SystemPathsSource(
            [SystemPath(path=tmp_path / "a"), SystemPath(path=tmp_path / "b", include=["x*"])]
        )
        assert source.targets() == [tmp_path / "a"]

    def test_nothing_to_back_up(self, tmp_path: Path) -> None:
        item = SystemPathsSource([SystemPath(path=tmp_path, include=["x*"])]).backup(
            ctx(FakeRestic())
        )
        assert item.error == "no file matches system.paths"

    def test_missing_path(self, tmp_path: Path) -> None:
        item = SystemPathsSource([SystemPath(path=tmp_path / "nope")]).backup(ctx(FakeRestic()))
        assert item.status == "failed"
        assert item.error == f"system path(s) not found: {tmp_path / 'nope'}"


class TestInventory:
    @pytest.fixture
    def commands(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
        outputs = {
            "dpkg": "bash\tinstall\ncurl\t\t\tdeinstall\nbroken-line\n",
            "apt-mark": "curl\ndocker-ce\n",
            "docker": "29.8.0\n",
        }

        def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
            if args[0] not in outputs:
                raise MissingDependencyError(f"command not found: {args[0]}")
            return CommandResult(tuple(args), 0, outputs[args[0]], "", 0.0)

        monkeypatch.setattr(runner, "run", run)
        release = tmp_path / "os-release"
        release.write_text('NAME="Ubuntu"\nVERSION_ID="24.04"\n# comment\nID=ubuntu\n')
        monkeypatch.setattr(system, "OS_RELEASE", release)
        return outputs

    def test_inventory_snapshot(self, commands: dict[str, str]) -> None:
        restic = FakeRestic()
        item = SystemInventorySource().backup(ctx(restic))
        assert (item.status, item.snapshot, item.label) == ("ok", "inv1", "system/inventory")
        document = json.loads(restic.documents["system/inventory.json"])
        assert document["os_release"] == {"NAME": "Ubuntu", "VERSION_ID": "24.04", "ID": "ubuntu"}
        assert document["packages"] == {"bash": "install", "curl": "deinstall"}
        assert document["manual_packages"] == ["curl", "docker-ce"]
        assert document["docker"] == "29.8.0"

    def test_missing_tools_leave_fields_empty(
        self, commands: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        del commands["dpkg"], commands["apt-mark"]
        monkeypatch.setattr(system, "OS_RELEASE", tmp_path / "missing")
        inventory = system.collect_inventory()
        assert inventory["packages"] is None
        assert inventory["manual_packages"] is None
        assert inventory["os_release"] is None
