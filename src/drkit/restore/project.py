"""``drkit restore project`` (DESIGN §7.2): code, secrets, images, databases, volumes, services.

The project directory is rebuilt, not copied: the code is cloned again at the recorded commit
(plus the patch of uncommitted changes), and only what git cannot give back comes from the
repository. Everything is taken from one run manifest, so the code, the database schema and
the uploaded files belong together.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from drkit import docker, git, health
from drkit.config import LoadedConfig, Project
from drkit.errors import DrkitError
from drkit.orchestrator import run_hooks
from drkit.restic import Node, Restic
from drkit.restore.database import (
    DatabaseTarget,
    check_database,
    connect,
    database_targets,
    restore_database,
)
from drkit.restore.steps import RestorePlan, Step
from drkit.runmanifest import ProjectState, RunManifest, find_run, new_run_id
from drkit.sources import volume as volume_source
from drkit.sources.base import ItemResult

UP_TIMEOUT_S = 900.0
# Seconds `docker compose up --wait` gives the database services to report healthy.
DATABASE_WAIT_S = 300
SECRET_MODE = 0o600


@dataclass(frozen=True, slots=True)
class ProjectRestore:
    """A project restore resolved from a run manifest: what comes from where."""

    project: Project
    run: RunManifest
    state: ProjectState
    destination: Path
    files: ItemResult
    """The ``project`` item: compose files (and the patch, through ``state``)."""
    secrets: ItemResult | None
    databases: list[DatabaseTarget]
    volumes: list[ItemResult]

    @property
    def sha(self) -> str:
        assert self.state.sha is not None  # noqa: S101 - checked when resolved
        return self.state.sha

    def compose(self, *args: str, timeout: float = UP_TIMEOUT_S) -> None:
        docker.compose_run(
            self.project.compose_project,
            self.project.compose_files(self.destination),
            self.destination,
            args,
            timeout=timeout,
        )

    def compose_config(self) -> dict[str, object]:
        return docker.compose_config(
            self.project.compose_project,
            self.project.compose_files(self.destination),
            self.destination,
        )


def resolve(project: Project, run: RunManifest, destination: Path) -> ProjectRestore:
    """Check that ``run`` holds a complete, restorable copy of ``project``."""
    state = run.projects.get(project.name)
    files = _item(run, "project", project.name)
    if files is None or state is None or state.sha is None:
        raise DrkitError(
            f"run {run.run_id} has no project state of {project.name!r}",
            hint="pick another run with --run (see `drkit runs list`)",
        )
    secrets = _item(run, "secrets", project.name)
    volumes = [i for i in run.items if i.kind == "volume" and i.project == project.name]
    for item in [files, *([secrets] if secrets else []), *volumes]:
        if item.status != "ok":
            raise DrkitError(
                f"{item.label} failed in run {run.run_id} and cannot be restored from it",
                hint="pick another run with --run (see `drkit runs list`)",
            )
    return ProjectRestore(
        project=project,
        run=run,
        state=state,
        destination=destination,
        files=files,
        secrets=secrets,
        databases=database_targets(project, run),
        volumes=volumes,
    )


def plan(
    loaded: LoadedConfig,
    project_name: str,
    restic: Restic,
    *,
    run_id: str = "latest",
    to_path: Path | None = None,
) -> RestorePlan:
    """Plan the nine steps of ``drkit restore project`` (DESIGN §7.2)."""
    project = loaded.manifest.project(project_name)
    destination = (to_path or project.path).absolute()
    run = find_run(
        restic,
        run_id,
        accept=lambda run: (
            project.name in run.projects and _item(run, "project", project.name) is not None
        ),
        what=f"project {project.name!r}",
    )
    target = resolve(project, run, destination)
    restore_id = new_run_id()
    aside = destination.with_name(f"{destination.name}.pre-restore-{restore_id}")
    steps = [
        Step(
            "Clone the repository at the recorded commit",
            lambda: clone(restic, target, aside),
            _clone_plan(target, aside),
        ),
        Step(
            "Restore compose files and secrets",
            lambda: restore_files(restic, target),
            _files_plan(target),
        ),
        Step("Pull the images", lambda: pull_images(target), _images_plan(target)),
        Step(
            "Start the database services",
            lambda: start_databases(target),
            _services_plan(target),
        ),
        Step(
            "Restore the databases",
            lambda: restore_databases(restic, target),
            tuple(f"{t.label}: roles, then the dump, then sanity checks" for t in target.databases)
            or ("no database",),
        ),
        Step(
            "Restore the volumes",
            lambda: restore_volumes(restic, target),
            tuple(
                f"{v.label}: snapshot {(v.snapshot or '')[:8]}, replacing the volume contents "
                "(services using it stay stopped)"
                for v in target.volumes
            )
            or ("no volume",),
        ),
        Step(
            "Start the project",
            lambda: start_project(target),
            ("docker compose up --detach --force-recreate --remove-orphans",),
        ),
        Step(
            "Run post_restore hooks",
            lambda: post_restore_hooks(target),
            tuple(project.hooks.post_restore) or ("none configured",),
        ),
        Step(
            "Healthchecks",
            lambda: healthchecks(target),
            tuple(
                f"GET {check.url} → {check.expect_status} within {check.timeout}s"
                for check in project.healthchecks
            )
            or ("none configured",),
        ),
    ]
    summary = (
        f"Restore project {project.name} from run {run.run_id} into {destination}, "
        "replacing its databases and volumes"
    )
    if _occupied(destination):
        summary += f"; the current {destination} is moved to {aside}"
    return RestorePlan(
        kind="project",
        target=project.name,
        run_id=run.run_id,
        steps=steps,
        summary=summary + ".",
        warnings=_warnings(target),
        restore_id=restore_id,
    )


# --------------------------------------------------------------------------- steps


def clone(restic: Restic, target: ProjectRestore, aside: Path) -> str:
    """Step 1: a fresh clone at the recorded commit, plus the patch of uncommitted changes."""
    destination, project = target.destination, target.project
    details = []
    if _occupied(destination):
        # Never deleted: it may hold the only copy of something (logs, untracked files).
        try:
            destination.rename(aside)
        except OSError as exc:
            raise DrkitError(
                f"cannot move {destination} aside to {aside}: {exc.strerror}",
                hint="move or remove it by hand, then resume with --from-step 1",
            ) from None
        details.append(f"previous directory moved to {aside}")
    elif destination.is_dir():
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    git.clone(project.git.remote, destination, project.git.deploy_key)
    git.checkout(destination, target.sha)
    details.append(f"commit {target.sha[:12]} checked out in {destination}")
    if target.state.patch_snapshot:
        restic.dump_into(
            target.state.patch_snapshot,
            f"/{project.name}/project/dirty.patch",
            git.args(destination, "apply", "--binary"),
            label="git apply",
        )
        details.append("uncommitted changes applied")
    return "; ".join(details)


def restore_files(restic: Restic, target: ProjectRestore) -> str:
    """Step 2: compose files and secrets, back in place with their original owners."""
    project, destination = target.project, target.destination
    assert target.files.snapshot is not None  # noqa: S101 - ok items have one
    compose = _restore_files(restic, target.files.snapshot, project.path, destination)
    if compose and os.geteuid() == 0:
        # The clone belongs to root; the deploy user owned the checkout, as its compose files.
        _chown_tree(destination, compose[0].uid, compose[0].gid)
    secrets: list[Node] = []
    if target.secrets is not None and target.secrets.snapshot is not None:
        secrets = _restore_files(restic, target.secrets.snapshot, project.path, destination)
        for node in secrets:
            path = destination / Path(node.path).relative_to(project.path)
            path.chmod(SECRET_MODE)
            if os.geteuid() == 0:
                os.chown(path, node.uid, node.gid)

    # The restored files must form a valid project before anything is started from them.
    config = target.compose_config()
    external = sorted(docker.external_volumes(config) & {volume.name for volume in target.volumes})
    if external:
        raise volume_source.external_volume_error(project.name, external[0])
    return (
        f"{len(compose)} compose file(s), "
        f"{len(secrets)} secret file(s) (mode {SECRET_MODE:04o}, original owner)"
    )


def pull_images(target: ProjectRestore) -> str:
    """Step 3: recorded images by digest; the other services by the tag in the compose files."""
    services = sorted(_services(target))
    pinned = {
        service: image
        for service, image in target.state.images.items()
        if service in services and "@sha256:" in image
    }
    for image in sorted(set(pinned.values())):
        docker.pull_pinned(image)
    by_tag = [service for service in services if service not in pinned]
    if by_tag:
        target.compose("pull", "--quiet", "--ignore-buildable", *by_tag)
    return f"{len(pinned)} service(s) pinned by digest, {len(by_tag)} pulled by tag"


def start_databases(target: ProjectRestore) -> str:
    """Step 4: only the database services, and wait until they accept connections."""
    services = list(dict.fromkeys(t.service for t in target.databases))
    if not services:
        return "no database service"
    target.compose(
        *("up", "--detach", "--force-recreate"),
        *("--wait", "--wait-timeout", str(DATABASE_WAIT_S)),
        *services,
    )
    # A healthcheck can pass while the entrypoint still initializes a fresh volume with a
    # temporary server: wait for the real one.
    for database in target.databases:
        connect(database).wait_ready()
    return f"{', '.join(services)} ready"


def restore_databases(restic: Restic, target: ProjectRestore) -> str:
    """Step 5: roles, then each dump, then sanity checks."""
    details = []
    for database in target.databases:
        restore_database(restic, database, roles=True)
        details.append(check_database(database))
    return "; ".join(details) or "no database"


def restore_volumes(restic: Restic, target: ProjectRestore) -> str:
    """Step 6: replace each volume's contents; the services using it stay stopped."""
    if not target.volumes:
        return "no volume"
    # Creates the project's volumes (and containers, without starting them) where missing.
    target.compose("up", "--no-start")
    details = []
    for item in target.volumes:
        assert item.snapshot is not None  # noqa: S101 - ok items have one
        name = volume_source.resolve(target.project, item.name, target.destination)
        mountpoint = docker.volume_mountpoint(name)
        if not (volume_source.readable(mountpoint) and os.access(mountpoint, os.W_OK)):
            raise DrkitError(
                f"cannot write volume {name} at {mountpoint}",
                hint="run drkit as root on the Docker host: volume data must be reachable "
                "from the host filesystem (Docker Desktop keeps it inside its VM)",
            )
        running = docker.volume_containers(name)
        if running:
            docker.stop_containers(running)  # started again by the next step
        source = restic.snapshot(item.snapshot).paths[0]
        restic.restore(item.snapshot, source, mountpoint, delete=True)
        details.append(f"{item.label} → {name}")
    return "; ".join(details)


def start_project(target: ProjectRestore) -> str:
    """Step 7: every service, recreated so none keeps a mount of the replaced directory."""
    target.compose("up", "--detach", "--force-recreate", "--remove-orphans")
    return f"{len(_services(target))} service(s) started"


def post_restore_hooks(target: ProjectRestore) -> str:
    """Step 8: e.g. migrations, from the restored project directory."""
    hooks = target.project.hooks.post_restore
    if not hooks:
        return "none configured"
    errors = run_hooks(
        target.project, "post_restore", stop_on_failure=True, directory=target.destination
    )
    if errors:
        raise DrkitError(errors[0])
    return f"{len(hooks)} hook(s) succeeded"


def healthchecks(target: ProjectRestore) -> str:
    """Step 9: the restore is done when the project answers."""
    if not target.project.healthchecks:
        return "none configured"
    results = health.wait_all(target.project.healthchecks)
    failed = [result for result in results if not result.ok]
    if failed:
        raise DrkitError(
            "; ".join(f"{result.url}: {result.error}" for result in failed),
            hint="check the service logs (`docker compose logs`)",
        )
    return "; ".join(
        f"{result.url}: HTTP {result.status_code} after {result.elapsed_s:g}s" for result in results
    )


# --------------------------------------------------------------------------- helpers


def _item(run: RunManifest, kind: str, project: str) -> ItemResult | None:
    return next((i for i in run.items if i.kind == kind and i.project == project), None)


def _occupied(path: Path) -> bool:
    if not path.exists():
        return False
    return not path.is_dir() or any(path.iterdir())


def _services(target: ProjectRestore) -> set[str]:
    services = target.compose_config().get("services") or {}
    assert isinstance(services, dict)  # noqa: S101 - compose models map service names
    return set(services)


def _restore_files(restic: Restic, snapshot_id: str, source: Path, destination: Path) -> list[Node]:
    """Restore a snapshot of files under ``source`` into ``destination``; their nodes."""
    snapshot = restic.snapshot(snapshot_id)
    outside = [path for path in snapshot.paths if not Path(path).is_relative_to(source)]
    if outside:
        raise DrkitError(
            f"snapshot {snapshot_id[:8]} holds {outside[0]}, outside the project directory "
            f"{source}",
            hint="projects[].path changed since this backup: set it back to restore this run",
        )
    restic.restore(snapshot_id, str(source), str(destination))
    return [
        node
        for node in restic.ls(snapshot_id)
        if node.type == "file" and Path(node.path).is_relative_to(source)
    ]


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    os.lchown(root, uid, gid)
    for directory, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            os.lchown(Path(directory) / name, uid, gid)


def _clone_plan(target: ProjectRestore, aside: Path) -> tuple[str, ...]:
    lines = [
        f"git clone {target.project.git.remote} into {target.destination}",
        f"check out commit {target.sha[:12]}",
    ]
    if target.state.patch_snapshot:
        lines.append(f"apply uncommitted changes (snapshot {target.state.patch_snapshot[:8]})")
    if _occupied(target.destination):
        lines.insert(0, f"move the current {target.destination} to {aside}")
    return tuple(lines)


def _files_plan(target: ProjectRestore) -> tuple[str, ...]:
    files = ", ".join(str(file) for file in target.project.compose.files)
    lines = [f"compose files ({files}) from snapshot {(target.files.snapshot or '')[:8]}"]
    if target.secrets is not None:
        lines.append(
            f"secrets from snapshot {(target.secrets.snapshot or '')[:8]}, "
            f"mode {SECRET_MODE:04o}, original owner"
        )
    return tuple(lines)


def _images_plan(target: ProjectRestore) -> tuple[str, ...]:
    return tuple(
        f"{service}: {image}" for service, image in sorted(target.state.images.items())
    ) or ("images by the tags in the compose files",)


def _services_plan(target: ProjectRestore) -> tuple[str, ...]:
    services = list(dict.fromkeys(t.service for t in target.databases))
    if not services:
        return ("no database service",)
    return (f"docker compose up --detach --wait {' '.join(services)}",)


def _warnings(target: ProjectRestore) -> list[str]:
    project, state = target.project, target.state
    warnings = []
    if state.remote and state.remote != project.git.remote:
        warnings.append(
            f"the checkout's origin was {state.remote}; cloning git.remote "
            f"{project.git.remote} instead"
        )
    if state.dirty:
        warnings.append(
            "the checkout had local changes: tracked ones come back from the patch, untracked "
            "files were not backed up"
        )
    in_run = {item.name for item in target.volumes}
    warnings += [
        f"volume {volume.name!r} is not in run {target.run.run_id}: its contents are left as "
        "they are"
        for volume in project.volumes
        if volume.name not in in_run
    ]
    if project.secrets and target.secrets is None:
        warnings.append(f"run {target.run.run_id} has no secrets of {project.name!r}")
    warnings += [
        f"service {service!r}: image {image} has no registry digest (built locally?)"
        for service, image in sorted(state.images.items())
        if "@sha256:" not in image
    ]
    return warnings
