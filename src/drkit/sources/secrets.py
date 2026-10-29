"""Project secrets (``.env`` files, keys): plain files next to the code, never in git."""

from __future__ import annotations

from typing import Any

from drkit.config import Project
from drkit.errors import DrkitError
from drkit.sources.base import BackupContext, BaseSource


class SecretsSource(BaseSource):
    kind = "secrets"

    def __init__(self, project: Project) -> None:
        super().__init__(project=project.name, name=project.name)
        self.paths = [project.path / secret for secret in project.secrets]

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        missing = [str(path) for path in self.paths if not path.exists()]
        if missing:
            raise DrkitError(
                f"secret file(s) not found: {', '.join(missing)}",
                hint="restore them, or remove them from projects[].secrets",
            )
        summary = ctx.restic.backup_paths([str(path) for path in self.paths], self.tags(ctx))
        return {"snapshot": summary.snapshot_id, "bytes": summary.bytes_processed}
