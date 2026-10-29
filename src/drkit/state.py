"""Local state (``state.json``): outcome of the last runs, for reports and RPO.

The repository stays the source of truth; this file only avoids a network round trip for
"when did the last backup succeed?".
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

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

    @classmethod
    def from_manifest(cls, manifest: RunManifest) -> RunRecord:
        return cls(
            run_id=manifest.run_id,
            status=manifest.status,
            started_at=manifest.started_at,
            finished_at=manifest.finished_at,
        )


class State(BaseModel):
    # Unknown keys are dropped rather than rejected: a newer drkit may have written them.
    model_config = ConfigDict(extra="ignore")

    last_run: RunRecord | None = None
    last_success: RunRecord | None = None


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


def record_run(manifest: RunManifest, directory: Path | None = None) -> State:
    current = load(directory)
    record = RunRecord.from_manifest(manifest)
    updated = State(
        last_run=record,
        last_success=record if manifest.status == "ok" else current.last_success,
    )
    save(updated, directory)
    return updated
