"""Host inventory: OS release and installed packages, to compare a rebuilt host against.

Only metadata: the host itself is rebuilt by Ansible (ADR-0002), not restored from here.
"""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from drkit import runner
from drkit.errors import DrkitError
from drkit.sources.base import BackupContext, BaseSource

INVENTORY_FILENAME = "system/inventory.json"
COMMAND_TIMEOUT_S = 120.0
OS_RELEASE = Path("/etc/os-release")

log = structlog.get_logger(__name__)


class SystemInventorySource(BaseSource):
    kind = "system"

    def __init__(self) -> None:
        super().__init__(project=None, name="inventory")

    def run(self, ctx: BackupContext) -> dict[str, Any]:
        document = json.dumps(collect_inventory(), indent=2, sort_keys=True) + "\n"
        summary = ctx.restic.backup_bytes(document, INVENTORY_FILENAME, self.tags(ctx))
        return {"snapshot": summary.snapshot_id, "bytes": summary.bytes_processed}


def collect_inventory() -> dict[str, Any]:
    """OS, kernel and packages. A missing tool (e.g. no dpkg) leaves its field null."""
    selections = _command(["dpkg", "--get-selections"])
    manual = _command(["apt-mark", "showmanual"])
    return {
        "collected_at": datetime.now(UTC).isoformat(),
        "os_release": parse_os_release(_read(OS_RELEASE)),
        "kernel": platform.release(),
        "docker": _command(["docker", "version", "--format", "{{.Server.Version}}"]),
        "packages": parse_selections(selections) if selections is not None else None,
        "manual_packages": manual.split() if manual is not None else None,
    }


def parse_os_release(text: str | None) -> dict[str, str] | None:
    if text is None:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("\"'")
    return values


def parse_selections(text: str) -> dict[str, str]:
    """``dpkg --get-selections`` → ``{package: state}``."""
    packages: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2:
            packages[fields[0]] = fields[1]
    return packages


def _command(args: list[str]) -> str | None:
    try:
        return runner.run(args, timeout=COMMAND_TIMEOUT_S).stdout.strip()
    except DrkitError as exc:
        log.warning("system.inventory_incomplete", command=args[0], error=exc.message)
        return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
