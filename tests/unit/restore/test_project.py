import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from drkit import docker, git, health
from drkit.config import LoadedConfig, Manifest
from drkit.errors import DrkitError
from drkit.health import HealthResult
from drkit.restic import Node, Restic, Snapshot
from drkit.restore import project as project_module
from drkit.restore import steps
from drkit.restore.database import DatabaseTarget
from drkit.runmanifest import ProjectState, RunManifest
from drkit.sources import volume as volume_source
from drkit.sources.base import ItemResult

RUN_ID = "20261029T030000Z-a1f3"
T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)
SHA = "9c1e2b7d" * 5
REMOTE = "git@example.com:shop.git"

FILES = ItemResult(kind="project", project="shop", name="shop", snapshot="files1", status="ok")
SECRETS = ItemResult(kind="secrets", project="shop", name="shop", snapshot="secrets1", status="ok")
DB = ItemResult(
    kind="db",
    project="shop",
    name="shop",
    engine="postgres",
    engine_version="16.9",
    snapshot="dump1",
    globals_snapshot="roles1",
    status="ok",
)
UPLOADS = ItemResult(kind="volume", project="shop", name="uploads", snapshot="vol1", status="ok")
STATE = ProjectState(
    remote=REMOTE,
    sha=SHA,
    dirty=True,
    images={"db": "postgres:16.9@sha256:aaa", "app": "ghcr.io/acme/shop:1@sha256:bbb"},
    patch_snapshot="patch1",
)


def manifest(path: Path, **project: Any) -> LoadedConfig:
    data = {
        "version": 1,
        "host": "prod-01",
        "repository": {"url": "s3:https://s3.example.com/b", "password_file": "/pw"},
        "retention": {"daily": 7},
        "projects": [
            {
                "name": "shop",
                "path": str(path),
                "git": {"remote": REMOTE},
                "compose": {"project_name": "shop-prod", "files": ["compose.yaml"]},
                "secrets": [".env"],
                "databases": [{"engine": "postgres", "service": "db", "database": "shop"}],
                "volumes": [{"name": "uploads"}],
                "hooks": {"post_restore": ["touch migrated"]},
                "healthchecks": [{"url": "http://127.0.0.1:8080/health"}],
                **project,
            }
        ],
    }
    return LoadedConfig(path=Path("/etc/drkit/dr.yaml"), manifest=Manifest.model_validate(data))


def run_with(*items: ItemResult, state: ProjectState | None = STATE) -> RunManifest:
    return RunManifest(
        run_id=RUN_ID,
        host="prod-01",
        drkit_version="0.3.0",
        started_at=T0,
        finished_at=T0,
        status="ok",
        items=list(items),
        projects={"shop": state} if state else {},
    )


@dataclass
class FakeRestic:
    """A repository holding the files of ``/srv/shop`` as the project backed them up."""

    origin: Path
    calls: list[str] = field(default_factory=list)
    files: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            "files1": {"compose.yaml": "services: {}\n"},
            "secrets1": {".env": "APP_KEY=secret\n"},
        }
    )

    def snapshot(self, snapshot_id: str) -> Snapshot:
        paths = [str(self.origin / name) for name in self.files.get(snapshot_id, {})]
        if snapshot_id == "vol1":
            paths = ["/var/lib/docker/volumes/shop-prod_uploads/_data"]
        return Snapshot(snapshot_id, snapshot_id, T0, "prod-01", (), tuple(paths))

    def restore(self, snapshot_id: str, path: str, target: str, *, delete: bool = False) -> None:
        self.calls.append(f"restore {snapshot_id}:{path} {target}{' --delete' if delete else ''}")
        for name, content in self.files.get(snapshot_id, {}).items():
            (Path(target) / name).write_text(content)

    def ls(self, snapshot_id: str) -> list[Node]:
        nodes = [Node(str(self.origin), "dir", 0, 0)]
        nodes += [
            Node(str(self.origin / name), "file", 1000, 33)
            for name in self.files.get(snapshot_id, {})
        ]
        return nodes

    def dump_into(
        self, snapshot_id: str, path: str, consumer: Sequence[str], **kwargs: Any
    ) -> None:
        self.calls.append(f"dump {snapshot_id}:{path} | {' '.join(consumer[-2:])}")


@dataclass
class FakeHost:
    """git, Docker and the database helpers, recording what the restore asks of them."""

    events: list[str] = field(default_factory=list)
    services: dict[str, Any] = field(default_factory=lambda: {"db": {}, "app": {}, "worker": {}})
    external: set[str] = field(default_factory=set)
    mountpoint: Path = Path("/nonexistent")
    hook_error: str | None = None
    health: list[HealthResult] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git, "clone", self.clone)
        monkeypatch.setattr(
            git, "checkout", lambda path, sha: self.events.append(f"checkout {sha[:8]}")
        )
        monkeypatch.setattr(docker, "compose_run", self.compose_run)
        monkeypatch.setattr(docker, "compose_config", self.compose_config)
        monkeypatch.setattr(docker, "pull_pinned", lambda ref: self.events.append(f"pull {ref}"))
        monkeypatch.setattr(docker, "volume_mountpoint", lambda name: str(self.mountpoint))
        monkeypatch.setattr(docker, "volume_containers", lambda name: ["app1"])
        monkeypatch.setattr(docker, "stop_containers", lambda c: self.events.append(f"stop {c}"))
        monkeypatch.setattr(volume_source, "resolve", lambda p, name, d: f"shop-prod_{name}")
        monkeypatch.setattr(project_module, "connect", self.connect)
        monkeypatch.setattr(project_module, "restore_database", self.restore_database)
        monkeypatch.setattr(project_module, "check_database", lambda t: f"{t.label}: 3 table(s)")
        monkeypatch.setattr(health, "wait_all", lambda checks: self.health)

    def clone(self, remote: str, destination: Path, deploy_key: Path | None) -> None:
        self.events.append(f"clone {remote}")
        destination.mkdir()
        (destination / "compose.yaml").write_text("from git\n")

    def compose_run(
        self, project: str, files: Sequence[Path], directory: Path, args: Sequence[str], **kw: Any
    ) -> None:
        self.events.append(f"compose {' '.join(args)}")

    def compose_config(self, project: str, files: Sequence[Path], directory: Path) -> Any:
        volumes = {name: {"external": True} for name in self.external}
        return {"services": self.services, "volumes": volumes}

    def connect(self, target: DatabaseTarget) -> Any:
        events = self.events

        class Engine:
            def wait_ready(self) -> None:
                events.append(f"ready {target.label}")

        return Engine()

    def restore_database(self, restic: Any, target: DatabaseTarget, *, roles: bool) -> str:
        self.events.append(f"restore {target.label} roles={roles}")
        return target.label


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHost:
    fake = FakeHost(mountpoint=tmp_path / "volume")
    fake.mountpoint.mkdir()
    fake.health = [HealthResult("http://127.0.0.1:8080/health", True, 2, 1.5, 200)]
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return tmp_path / "srv" / "shop"


@pytest.fixture
def restic(origin: Path) -> FakeRestic:
    return FakeRestic(origin=origin)


def plan(
    restic: FakeRestic,
    loaded: LoadedConfig,
    run: RunManifest,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> steps.RestorePlan:
    def find_run(repo: Any, run_id: str, *, accept: Any, what: str) -> RunManifest:
        assert accept(run), what
        return run

    monkeypatch.setattr(project_module, "find_run", find_run)
    return project_module.plan(loaded, "shop", cast(Restic, restic), **kwargs)


def test_plan_has_the_nine_steps(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = run_with(FILES, SECRETS, DB, UPLOADS)
    restore_plan = plan(restic, manifest(origin), run, monkeypatch)

    assert [step.title for step in restore_plan.steps] == [
        "Clone the repository at the recorded commit",
        "Restore compose files and secrets",
        "Pull the images",
        "Start the database services",
        "Restore the databases",
        "Restore the volumes",
        "Start the project",
        "Run post_restore hooks",
        "Healthchecks",
    ]
    assert restore_plan.steps[0].plan == (
        f"git clone {REMOTE} into {origin}",
        f"check out commit {SHA[:12]}",
        "apply uncommitted changes (snapshot patch1)",
    )
    assert restore_plan.steps[1].plan == (
        "compose files (compose.yaml) from snapshot files1",
        "secrets from snapshot secrets1, mode 0600, original owner",
    )
    assert restore_plan.steps[2].plan == (
        "app: ghcr.io/acme/shop:1@sha256:bbb",
        "db: postgres:16.9@sha256:aaa",
    )
    assert restore_plan.steps[3].plan == ("docker compose up --detach --wait db",)
    assert restore_plan.steps[8].plan == ("GET http://127.0.0.1:8080/health → 200 within 60s",)
    assert restore_plan.summary == (
        f"Restore project shop from run {RUN_ID} into {origin}, replacing its databases and "
        "volumes."
    )
    assert restore_plan.warnings == [
        "the checkout had local changes: tracked ones come back from the patch, untracked "
        "files were not backed up"
    ]
    assert host.events == [], "planning changes nothing"


def test_full_restore(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = run_with(FILES, SECRETS, DB, UPLOADS)
    restore_plan = plan(restic, manifest(origin), run, monkeypatch)

    report = steps.execute(restore_plan)

    assert report.ok, report.steps
    assert [s.detail for s in report.steps] == [
        f"commit {SHA[:12]} checked out in {origin}; uncommitted changes applied",
        "1 compose file(s), 1 secret file(s) (mode 0600, original owner)",
        "2 service(s) pinned by digest, 1 pulled by tag",
        "db ready",
        "shop/db/shop: 3 table(s)",
        "shop/volume/uploads → shop-prod_uploads",
        "3 service(s) started",
        "1 hook(s) succeeded",
        "http://127.0.0.1:8080/health: HTTP 200 after 1.5s",
    ]
    assert host.events == [
        f"clone {REMOTE}",
        f"checkout {SHA[:8]}",
        "pull ghcr.io/acme/shop:1@sha256:bbb",
        "pull postgres:16.9@sha256:aaa",
        "compose pull --quiet --ignore-buildable worker",
        "compose up --detach --force-recreate --wait --wait-timeout 300 db",
        "ready shop/db/shop",
        "restore shop/db/shop roles=True",
        "compose up --no-start",
        "stop ['app1']",
        "compose up --detach --force-recreate --remove-orphans",
    ]
    assert restic.calls == [
        "dump patch1:/shop/project/dirty.patch | apply --binary",
        f"restore files1:{origin} {origin}",
        f"restore secrets1:{origin} {origin}",
        f"restore vol1:/var/lib/docker/volumes/shop-prod_uploads/_data {host.mountpoint} --delete",
    ]
    # The backed-up compose file wins over the committed one; secrets are private again.
    assert (origin / "compose.yaml").read_text() == "services: {}\n"
    assert (origin / ".env").stat().st_mode & 0o777 == 0o600
    assert (origin / "migrated").exists()


def test_existing_directory_is_moved_aside(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.mkdir(parents=True)
    (origin / "uploads.log").write_text("only copy\n")
    restore_plan = plan(restic, manifest(origin), run_with(FILES), monkeypatch)
    aside = origin.with_name(f"shop.pre-restore-{restore_plan.restore_id}")
    assert restore_plan.summary.endswith(f"; the current {origin} is moved to {aside}.")
    assert restore_plan.steps[0].plan[0] == f"move the current {origin} to {aside}"

    report = steps.execute(restore_plan)

    assert report.steps[0].detail == (
        f"previous directory moved to {aside}; commit {SHA[:12]} checked out in {origin}; "
        "uncommitted changes applied"
    )
    assert (aside / "uploads.log").read_text() == "only copy\n"


def test_empty_directory_is_reused(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.mkdir(parents=True)
    restore_plan = plan(restic, manifest(origin), run_with(FILES), monkeypatch)
    assert "moved" not in restore_plan.summary
    assert steps.execute(restore_plan, from_step=1).steps[0].status == "ok"
    assert not list(origin.parent.glob("*.pre-restore-*"))


def test_unmovable_directory(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.mkdir(parents=True)
    (origin / "file").write_text("x")

    def rename(self: Path, target: Path) -> None:
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(Path, "rename", rename)
    report = steps.execute(plan(restic, manifest(origin), run_with(FILES), monkeypatch))
    assert report.steps[0].error is not None
    assert report.steps[0].error.startswith(f"cannot move {origin} aside to ")
    assert "--from-step 1" in report.steps[0].error


def test_restore_into_another_directory(
    host: FakeHost,
    restic: FakeRestic,
    origin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / "new" / "shop"
    restore_plan = plan(
        restic, manifest(origin), run_with(FILES, SECRETS), monkeypatch, to_path=elsewhere
    )
    report = steps.execute(restore_plan)
    assert report.ok, report.steps
    assert (elsewhere / ".env").read_text() == "APP_KEY=secret\n"
    assert f"restore files1:{origin} {elsewhere}" in restic.calls


def test_owners_are_restored_as_root(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chowned: dict[str, tuple[int, int]] = {}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "lchown", lambda p, u, g: chowned.__setitem__(str(p), (u, g)))
    monkeypatch.setattr(os, "chown", lambda p, u, g: chowned.__setitem__(f"chown {p}", (u, g)))

    report = steps.execute(
        plan(restic, manifest(origin), run_with(FILES, SECRETS), monkeypatch), from_step=1
    )

    assert report.steps[1].status == "ok", report.steps
    # The checkout goes to the owner of the compose files; each secret keeps its own owner.
    assert chowned[str(origin)] == (1000, 33)
    assert chowned[str(origin / "compose.yaml")] == (1000, 33)
    assert chowned[f"chown {origin / '.env'}"] == (1000, 33)


def test_external_volumes_are_refused_before_anything_starts(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.external = {"uploads"}
    report = steps.execute(plan(restic, manifest(origin), run_with(FILES, UPLOADS), monkeypatch))
    assert [s.status for s in report.steps[:3]] == ["ok", "failed", "not_run"]
    assert report.steps[1].error is not None
    assert "volume 'uploads' of project 'shop' is declared external" in report.steps[1].error


def test_unwritable_volume(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.mountpoint = host.mountpoint / "missing"
    report = steps.execute(
        plan(restic, manifest(origin), run_with(FILES, UPLOADS), monkeypatch), from_step=6
    )
    assert report.steps[5].error is not None
    assert report.steps[5].error.startswith("cannot write volume shop-prod_uploads at ")


def test_failed_hook_and_healthcheck(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.mkdir(parents=True)
    loaded = manifest(origin, hooks={"post_restore": ["exit 3"]})
    report = steps.execute(plan(restic, loaded, run_with(FILES), monkeypatch), from_step=8)
    assert report.steps[7].error == "post_restore hook failed with exit code 3: exit 3"

    host.health = [HealthResult("http://127.0.0.1:8080/health", False, 5, 60.0, 502, "HTTP 502")]
    report = steps.execute(plan(restic, loaded, run_with(FILES), monkeypatch), from_step=9)
    assert report.steps[8].error == (
        "http://127.0.0.1:8080/health: HTTP 502 (hint: check the service logs "
        "(`docker compose logs`))"
    )


def test_nothing_to_do_steps(
    host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = manifest(origin, databases=[], volumes=[], hooks={}, healthchecks=[], secrets=[])
    state = STATE.model_copy(update={"dirty": False, "patch_snapshot": None, "images": {}})
    restore_plan = plan(restic, loaded, run_with(FILES, state=state), monkeypatch)
    assert restore_plan.steps[2].plan == ("images by the tags in the compose files",)
    report = steps.execute(restore_plan)
    assert report.ok, report.steps
    assert [s.detail for s in report.steps][2:] == [
        "0 service(s) pinned by digest, 3 pulled by tag",
        "no database service",
        "no database",
        "no volume",
        "3 service(s) started",
        "none configured",
        "none configured",
    ]


class TestPlanChecks:
    def test_warnings(
        self, host: FakeHost, restic: FakeRestic, origin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = STATE.model_copy(
            update={
                "remote": "git@old.example.com:shop.git",
                "images": {"app": "shop-app:latest"},
            }
        )
        restore_plan = plan(restic, manifest(origin), run_with(FILES, DB, state=state), monkeypatch)
        assert restore_plan.warnings == [
            f"the checkout's origin was git@old.example.com:shop.git; cloning git.remote {REMOTE} "
            "instead",
            "the checkout had local changes: tracked ones come back from the patch, untracked "
            "files were not backed up",
            f"volume 'uploads' is not in run {RUN_ID}: its contents are left as they are",
            f"run {RUN_ID} has no secrets of 'shop'",
            "service 'app': image shop-app:latest has no registry digest (built locally?)",
        ]

    def test_run_without_project_state(
        self, host: FakeHost, restic: FakeRestic, origin: Path
    ) -> None:
        with pytest.raises(DrkitError, match=f"run {RUN_ID} has no project state of 'shop'"):
            project_module.resolve(
                manifest(origin).manifest.projects[0], run_with(FILES, state=None), origin
            )

    def test_failed_item(self, host: FakeHost, restic: FakeRestic, origin: Path) -> None:
        failed = UPLOADS.model_copy(update={"status": "failed", "snapshot": None})
        with pytest.raises(DrkitError, match="shop/volume/uploads failed in run"):
            project_module.resolve(
                manifest(origin).manifest.projects[0], run_with(FILES, failed), origin
            )

    def test_project_path_changed_since_the_backup(
        self, host: FakeHost, restic: FakeRestic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        moved = tmp_path / "srv" / "shop-v2"
        report = steps.execute(plan(restic, manifest(moved), run_with(FILES), monkeypatch))
        assert report.steps[1].error is not None
        assert report.steps[1].error.startswith(
            f"snapshot files1 holds {restic.origin / 'compose.yaml'}, outside the project "
            f"directory {moved}"
        )
