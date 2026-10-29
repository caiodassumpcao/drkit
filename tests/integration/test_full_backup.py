"""A full run (every item kind, hooks, stop consistency) against real Docker and MinIO."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from drkit import docker
from drkit.errors import CommandError
from drkit.restic import Restic
from drkit.runmanifest import RunManifest
from drkit.sources import volume as volume_module
from tests.integration.conftest import Stack

pytestmark = pytest.mark.integration

POSTGRES = {"engine": "postgres", "service": "db", "database": "shop"}


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A deployed project: a git clone with a local hotfix, an untracked file and a secret."""
    path = tmp_path / "shop"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "deploy@example.com")
    git(path, "config", "user.name", "Deploy")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "remote", "add", "origin", "git@example.com:shop.git")
    (path / "compose.yaml").write_text("services:\n  app:\n    image: busybox\n")
    (path / "app.py").write_text("print('v1')\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "release v1")
    (path / "app.py").write_text("print('hotfix')\n")
    (path / "notes.txt").write_text("untracked\n")
    (path / ".env").write_text("APP_KEY=drkit-test-app-key\n")
    return path


@pytest.fixture
def uploads(stack: Stack) -> str:
    return docker.volume_mountpoint(docker.compose_volume(stack.project, "uploads"))


def started_at(container: str) -> str:
    """``"<running> <start time>"``: a new start time proves the container was restarted."""
    return subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}} {{.State.StartedAt}}", container],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_full_run_contains_every_item_kind(
    make_drkit: Any, stack: Stack, checkout: Path, uploads: str, tmp_path: Path
) -> None:
    if not volume_module.readable(uploads):
        pytest.skip(f"volume data at {uploads} is not reachable from this host (run as root)")
    nginx = tmp_path / "nginx"
    (nginx / "sites-enabled").mkdir(parents=True)
    (nginx / "sites-enabled" / "shop.conf").write_text("server { listen 80; }\n")
    drkit = make_drkit(
        databases={"shop": [POSTGRES]},
        projects={
            "shop": {
                "path": str(checkout),
                "git": {"remote": "git@example.com:shop.git"},
                "secrets": [".env"],
                "volumes": [{"name": "uploads", "consistency": "stop"}],
                "hooks": {
                    "pre_backup": ["echo pre >> hooks.log"],
                    "post_backup": ["echo post >> hooks.log"],
                },
            }
        },
        system={"paths": [{"path": str(nginx)}]},
    )
    drkit("init")
    app = docker.compose_container(stack.project, "app")
    before = started_at(app)

    result = drkit("backup")

    assert result.exit_code == 0, result.output
    run = RunManifest.from_json(drkit("--json", "runs", "show").stdout)
    assert run.status == "ok"
    assert {item.label: item.status for item in run.items} == {
        "shop/db/shop": "ok",
        "shop/volume/uploads": "ok",
        "shop/secrets": "ok",
        "shop/project": "ok",
        "system/paths": "ok",
        "system/inventory": "ok",
    }
    items = {item.label: item for item in run.items}

    # Hooks bracket the project; the stopped service is running again, restarted.
    assert (checkout / "hooks.log").read_text().split() == ["pre", "post"]
    after = started_at(app)
    assert after.startswith("true ")
    assert after != before

    restic: Restic = drkit.restic()

    def content(label: str, path: str) -> str:
        snapshot = items[label].snapshot
        assert snapshot is not None
        return restic.dump(snapshot, path)

    assert content("shop/volume/uploads", f"{uploads}/hello.txt") == "drkit-seed-volume\n"
    assert "APP_KEY=drkit-test-app-key" in content("shop/secrets", f"{checkout}/.env")
    assert "busybox" in content("shop/project", f"{checkout}/compose.yaml")
    assert "listen 80" in content("system/paths", f"{nginx}/sites-enabled/shop.conf")
    inventory = json.loads(content("system/inventory", "/system/inventory.json"))
    assert inventory["kernel"]
    assert inventory["docker"]

    project = run.projects["shop"]
    assert project.sha == git(checkout, "rev-parse", "HEAD")
    assert project.remote == "git@example.com:shop.git"
    assert project.dirty is True
    assert project.patch_snapshot is not None
    patch = restic.dump(project.patch_snapshot, "/shop/project/dirty.patch")
    assert "+print('hotfix')" in patch
    assert "notes.txt" not in patch
    assert set(project.images) >= {"app", "db", "minio", "mysql", "mariadb"}
    assert all("@sha256:" in image for image in project.images.values())

    tags = {snap.id: set(snap.tags) for snap in restic.snapshots()}
    volume_snapshot = items["shop/volume/uploads"].snapshot
    assert volume_snapshot is not None
    assert {"kind:volume", "project:shop", "volume:uploads"} <= tags[volume_snapshot]


def test_stopped_services_restart_when_the_backup_fails(
    make_drkit: Any, stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fault injection: the volume looks readable (so the service is really stopped) and
    # restic fails while it is down.
    monkeypatch.setattr(volume_module, "readable", lambda path: True)
    app = docker.compose_container(stack.project, "app")
    states: list[str] = []

    def failing_backup(self: Restic, *args: Any, **kwargs: Any) -> Any:
        states.append(started_at(app))
        raise CommandError("restic backup failed with exit code 1: injected failure")

    monkeypatch.setattr(Restic, "backup_paths", failing_backup)
    drkit = make_drkit(
        databases={},
        projects={"shop": {"volumes": [{"name": "uploads", "consistency": "stop"}]}},
    )
    drkit("init")
    before = started_at(app)

    result = drkit("backup", "--only", "volume")

    assert result.exit_code == 1, result.output
    [item] = RunManifest.from_json(drkit("--json", "runs", "show").stdout).items
    assert (item.label, item.status) == ("shop/volume/uploads", "failed")
    assert item.error == "restic backup failed with exit code 1: injected failure"
    assert states[0].startswith("false "), "the service must be down during the backup"
    after = started_at(app)
    assert after.startswith("true ")
    assert after != before
