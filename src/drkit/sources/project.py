"""Project state: git commit, uncommitted changes, compose files and the images in use.

The code itself is not backed up: it is cloned again from ``git.remote`` at the recorded
commit. What git cannot give back is captured here.
"""

from __future__ import annotations

from typing import Any

import structlog

from drkit import docker, git, runner
from drkit.config import Project
from drkit.errors import CommandError, DrkitError
from drkit.runmanifest import ProjectState
from drkit.sources.base import BackupContext, BaseSource, stream_dump

log = structlog.get_logger(__name__)


class ProjectSource(BaseSource):
    kind = "project"

    def __init__(self, project: Project) -> None:
        super().__init__(project=project.name, name=project.name)
        self.config = project
        self.state: ProjectState | None = None
        """Recorded in the run manifest (``projects``) once the backup succeeded."""

    def git_args(self, *args: str) -> list[str]:
        return git.args(self.config.path, *args)

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        try:
            sha = runner.run(self.git_args("rev-parse", "HEAD"), timeout=git.GIT_TIMEOUT_S)
        except CommandError as exc:
            raise DrkitError(
                f"{self.config.path} is not a git checkout with a commit: {exc.message}",
                hint="projects[].path must be a clone of git.remote",
            ) from None
        remote = runner.run(
            self.git_args("remote", "get-url", "origin"), timeout=git.GIT_TIMEOUT_S, check=False
        )
        changes = runner.run(
            self.git_args("status", "--porcelain"), timeout=git.GIT_TIMEOUT_S
        ).stdout.splitlines()
        untracked = sum(line.startswith("??") for line in changes)
        modified = len(changes) - untracked
        if changes:
            # Uncommitted changes are captured as a patch; untracked files are not (only
            # the compose files and secrets listed in the manifest are).
            log.warning("project.dirty", item=self.label, modified=modified, untracked=untracked)
        remote_url = remote.stdout.strip() if remote.ok else None
        if remote_url != self.config.git.remote:
            log.warning("project.remote_mismatch", item=self.label)

        images = docker.project_images(self.config.compose_project)

        compose_files = [self.config.path / name for name in self.config.compose.files]
        missing = [str(path) for path in compose_files if not path.is_file()]
        if missing:
            raise DrkitError(
                f"compose file(s) not found: {', '.join(missing)}",
                hint="fix projects[].compose.files",
            )
        tags = self.tags(ctx)
        files = ctx.restic.backup_paths([str(path) for path in compose_files], tags)
        size = files.bytes_processed

        patch_snapshot = None
        if modified:
            try:
                patch = stream_dump(
                    ctx,
                    self.git_args("diff", "HEAD", "--binary"),
                    label="git diff",
                    filename=f"{self.project}/project/dirty.patch",
                    tags=tags,
                )
            except DrkitError:
                ctx.restic.forget([files.snapshot_id])
                raise
            patch_snapshot = patch.snapshot_id
            size += patch.bytes_processed

        self.state = ProjectState(
            remote=remote_url,
            sha=sha.stdout.strip(),
            dirty=bool(changes),
            images=images,
            patch_snapshot=patch_snapshot,
        )
        return {"snapshot": files.snapshot_id, "bytes": size}
