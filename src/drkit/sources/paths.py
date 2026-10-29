"""Host configuration paths (``system.paths``): nginx sites, systemd units, crontabs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from drkit.config import SystemPath
from drkit.errors import DrkitError
from drkit.sources.base import BackupContext, BaseSource

log = structlog.get_logger(__name__)


class SystemPathsSource(BaseSource):
    kind = "system"

    def __init__(self, paths: list[SystemPath]) -> None:
        super().__init__(project=None, name="paths")
        self.paths = paths

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        targets = self.targets()
        summary = ctx.restic.backup_paths([str(path) for path in targets], self.tags(ctx))
        return {"snapshot": summary.snapshot_id, "bytes": summary.bytes_processed}

    def targets(self) -> list[Path]:
        """Paths to back up: whole entries, or the files matching their ``include`` globs."""
        missing = [str(entry.path) for entry in self.paths if not entry.path.exists()]
        if missing:
            raise DrkitError(
                f"system path(s) not found: {', '.join(missing)}",
                hint="create them, or remove them from system.paths",
            )
        targets: list[Path] = []
        for entry in self.paths:
            if not entry.include:
                targets.append(entry.path)
                continue
            matches = sorted({m for pattern in entry.include for m in entry.path.glob(pattern)})
            if not matches:
                log.warning("system.no_match", path=str(entry.path), include=entry.include)
            targets += matches
        if not targets:
            raise DrkitError(
                "no file matches system.paths", hint="check the include patterns of system.paths"
            )
        return targets
