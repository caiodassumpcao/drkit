"""Run manifest: the record of one backup run and the starting point of every restore (ADR-0004).

Each run stores its manifest as its last snapshot (``kind:meta``, ``runs/<run_id>.json``). The
meta snapshot also carries a ``status:<status>`` tag, so runs can be listed without
downloading every manifest.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from drkit.errors import DrkitError
from drkit.restic import Restic, Snapshot
from drkit.sources.base import ItemResult

SCHEMA_VERSION: Literal[1] = 1
META_KIND = "meta"
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")

RunStatus = Literal["ok", "partial", "failed"]


class ProjectState(BaseModel):
    """Code and image state of a project at backup time (filled from v0.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    remote: str | None = None
    sha: str | None = None
    dirty: bool = False
    images: dict[str, str] = Field(default_factory=dict)


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1] = Field(default=SCHEMA_VERSION, alias="schema")
    run_id: str
    host: str
    drkit_version: str
    started_at: datetime
    finished_at: datetime
    status: RunStatus
    items: list[ItemResult] = Field(default_factory=list)
    projects: dict[str, ProjectState] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RunManifest:
        return cls.model_validate_json(text)

    @property
    def failed_items(self) -> list[ItemResult]:
        return [item for item in self.items if item.status == "failed"]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A run as seen from its meta snapshot (no manifest download needed)."""

    run_id: str
    status: str
    time: datetime
    snapshot_id: str

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> RunSummary:
        return cls(
            run_id=snapshot.tag_value("run") or "?",
            status=snapshot.tag_value("status") or "?",
            time=snapshot.time,
            snapshot_id=snapshot.id,
        )


def new_run_id(now: datetime | None = None) -> str:
    """``YYYYMMDDTHHMMSSZ-<4 hex>``: sortable, and unique even for runs in the same second."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{moment:%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def run_status(items: Iterable[ItemResult]) -> RunStatus:
    statuses = {item.status for item in items}
    if statuses == {"failed"}:
        return "failed"
    return "partial" if "failed" in statuses else "ok"


def manifest_filename(run_id: str) -> str:
    return f"runs/{run_id}.json"


def manifest_tags(host: str, run_id: str, status: RunStatus) -> list[str]:
    return ["drkit", f"host:{host}", f"run:{run_id}", f"kind:{META_KIND}", f"status:{status}"]


def list_runs(restic: Restic) -> list[RunSummary]:
    """All runs of the host, oldest first."""
    snapshots = restic.snapshots(["drkit", f"kind:{META_KIND}"])
    return [RunSummary.from_snapshot(s) for s in snapshots]


def load_run(restic: Restic, run_id: str = "latest") -> RunManifest:
    """Fetch the manifest of ``run_id`` (or of the most recent run)."""
    if run_id == "latest":
        snapshots = restic.snapshots(["drkit", f"kind:{META_KIND}"])
    else:
        if not RUN_ID_RE.match(run_id):
            raise DrkitError(
                f"invalid run ID {run_id!r}", hint="run IDs look like 20261027T030000Z-a1f3"
            )
        snapshots = restic.snapshots(["drkit", f"kind:{META_KIND}", f"run:{run_id}"])
    if not snapshots:
        raise DrkitError(
            "no backup run found" if run_id == "latest" else f"run {run_id} not found",
            hint="list the available runs with `drkit runs list`",
        )
    snapshot = snapshots[-1]
    found = snapshot.tag_value("run") or run_id
    return RunManifest.from_json(restic.dump(snapshot.id, f"/{manifest_filename(found)}"))
