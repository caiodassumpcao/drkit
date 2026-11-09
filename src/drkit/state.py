"""Local state (``state.json``): outcome of the last runs, for reports and RPO.

The repository stays the source of truth; this file only avoids a network round trip for
"when did the last backup succeed?".
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from drkit.errors import ConfigError
from drkit.runmanifest import RunManifest, RunStatus

DEFAULT_STATE_DIR = Path("/var/lib/drkit")
STATE_DIR_ENV_VAR = "DRKIT_STATE_DIR"
STATE_FILE = "state.json"


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    # Since 1.1; absent in state files written by 1.0.
    items_ok: int | None = None
    items_failed: int | None = None
    bytes_added: int | None = None
    """New data the run added to the repository (after deduplication)."""

    @classmethod
    def from_manifest(cls, manifest: RunManifest, *, bytes_added: int | None = None) -> RunRecord:
        failed = len(manifest.failed_items)
        return cls(
            run_id=manifest.run_id,
            status=manifest.status,
            started_at=manifest.started_at,
            finished_at=manifest.finished_at,
            items_ok=len(manifest.items) - failed,
            items_failed=failed,
            bytes_added=bytes_added,
        )


class ProjectRecord(BaseModel):
    """Last run in which every item of a project succeeded."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    last_success_at: datetime


class PruneRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "failed"]
    finished_at: datetime


class RestoreRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    restore_id: str
    status: Literal["ok", "failed"]
    finished_at: datetime
    rto_s: float | None = None
    """Host restores: start to last green healthcheck; db and project restores: duration.
    Only for successful restores."""


class VerifyRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["ok", "failed"]
    finished_at: datetime


class DrillRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    drill_id: str
    status: Literal["ok", "failed"]
    finished_at: datetime
    rto_s: float | None = None
    rpo_s: float | None = None


class State(BaseModel):
    # Unknown keys are dropped rather than rejected: a newer drkit may have written them.
    model_config = ConfigDict(extra="ignore")

    last_run: RunRecord | None = None
    last_success: RunRecord | None = None
    last_verify: VerifyRecord | None = None
    last_drill: DrillRecord | None = None
    # Since 1.1 (metrics); older state files simply lack them.
    projects: dict[str, ProjectRecord] = Field(default_factory=dict)
    last_prune: PruneRecord | None = None
    last_restores: dict[str, RestoreRecord] = Field(default_factory=dict)
    """Last restore of each kind: ``db``, ``project``, ``host``."""


def state_dir() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV_VAR) or DEFAULT_STATE_DIR)


def load(directory: Path | None = None) -> State:
    path = (directory or state_dir()) / STATE_FILE
    try:
        return State.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State()
    except (OSError, ValidationError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"cannot read state file {path}: {type(exc).__name__}",
            hint="fix or delete it; it is rebuilt by the next run",
        ) from None


def save(state: State, directory: Path | None = None) -> Path:
    """Write atomically (temp file + rename): a crash never leaves a truncated file."""
    target_dir = directory or state_dir()
    try:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".state.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(state.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        path = target_dir / STATE_FILE
        Path(tmp).replace(path)
    except OSError as exc:
        raise ConfigError(
            f"cannot write state file in {target_dir}: {exc.strerror}",
            hint=f"run drkit as root, or set ${STATE_DIR_ENV_VAR} to a writable directory",
        ) from None
    return path


def record_run(
    manifest: RunManifest, directory: Path | None = None, *, bytes_added: int | None = None
) -> State:
    current = load(directory)
    record = RunRecord.from_manifest(manifest, bytes_added=bytes_added)
    projects = dict(current.projects)
    for name in dict.fromkeys(i.project for i in manifest.items if i.project):
        if all(i.status == "ok" for i in manifest.items if i.project == name):
            projects[name] = ProjectRecord(
                run_id=manifest.run_id, last_success_at=manifest.finished_at
            )
    updated = current.model_copy(
        update={
            "last_run": record,
            "last_success": record if manifest.status == "ok" else current.last_success,
            "projects": projects,
        }
    )
    save(updated, directory)
    return updated


def record_prune(status: Literal["ok", "failed"], directory: Path | None = None) -> State:
    record = PruneRecord(status=status, finished_at=datetime.now(UTC))
    updated = load(directory).model_copy(update={"last_prune": record})
    save(updated, directory)
    return updated


def record_restore(kind: str, record: RestoreRecord, directory: Path | None = None) -> State:
    current = load(directory)
    updated = current.model_copy(update={"last_restores": {**current.last_restores, kind: record}})
    save(updated, directory)
    return updated


def record_verify(
    run_id: str, status: Literal["ok", "failed"], directory: Path | None = None
) -> State:
    record = VerifyRecord(run_id=run_id, status=status, finished_at=datetime.now(UTC))
    updated = load(directory).model_copy(update={"last_verify": record})
    save(updated, directory)
    return updated


def record_drill(record: DrillRecord, directory: Path | None = None) -> State:
    updated = load(directory).model_copy(update={"last_drill": record})
    save(updated, directory)
    return updated
