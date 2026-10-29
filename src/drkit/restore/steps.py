"""Restore pipeline: numbered steps, timed one by one, resumable, ending in a saved report.

A restore is a fixed list of steps planned from a run manifest before anything changes.
``--dry-run`` prints the plan, ``--from-step <n>`` skips the steps before ``n`` (to resume
after fixing a failure), and the first failing step stops the pipeline: later steps build on
earlier ones, so carrying on would only stack errors.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

from drkit import lock, runner, state
from drkit.errors import ConfigError, DrkitError
from drkit.runmanifest import new_run_id

REPORTS_DIR = "reports"

StepStatus = Literal["ok", "failed", "skipped", "not_run"]
RestoreKind = Literal["db", "project"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Step:
    title: str
    action: Callable[[], str | None]
    """Does the work; returns a short detail for the report."""
    plan: tuple[str, ...] = ()
    """What the step will do, as printed by ``--dry-run``."""


@dataclass(frozen=True, slots=True)
class RestorePlan:
    kind: RestoreKind
    target: str
    """What is restored: ``shop``, or ``shop/db/orders``."""
    run_id: str
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)
    """Things the operator should know before confirming (e.g. uncommitted changes)."""
    restore_id: str = field(default_factory=new_run_id)
    """Names the report and tags the safety dumps taken by this restore."""

    def check_from_step(self, from_step: int) -> None:
        if not 1 <= from_step <= len(self.steps):
            raise ConfigError(
                f"--from-step must be between 1 and {len(self.steps)}, got {from_step}",
                hint="see the numbered steps with --dry-run",
            )


@dataclass(slots=True)
class StepResult:
    number: int
    title: str
    status: StepStatus
    duration_s: float = 0.0
    detail: str | None = None
    error: str | None = None


@dataclass(slots=True)
class RestoreReport:
    restore_id: str
    kind: RestoreKind
    target: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    steps: list[StepResult] = field(default_factory=list)
    report_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    """Problems around the restore (saving the report) that leave its result unchanged."""

    @property
    def ok(self) -> bool:
        return all(step.status in ("ok", "skipped") for step in self.steps)

    @property
    def failed_step(self) -> StepResult | None:
        return next((step for step in self.steps if step.status == "failed"), None)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return round((end - self.started_at).total_seconds(), 1)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = "ok" if self.ok else "failed"
        data["duration_s"] = self.duration_s
        data["report_path"] = str(self.report_path) if self.report_path else None
        return data


def execute(
    plan: RestorePlan,
    *,
    from_step: int = 1,
    on_step: Callable[[StepResult], None] | None = None,
) -> RestoreReport:
    """Run the steps of ``plan`` from ``from_step`` on, stopping at the first failure."""
    plan.check_from_step(from_step)
    started = datetime.now(UTC)
    report = RestoreReport(
        restore_id=plan.restore_id,
        kind=plan.kind,
        target=plan.target,
        run_id=plan.run_id,
        started_at=started,
    )
    with structlog.contextvars.bound_contextvars(restore_id=report.restore_id):
        log.info("restore.start", kind=plan.kind, target=plan.target, run_id=plan.run_id)
        for number, step in enumerate(plan.steps, start=1):
            if number < from_step:
                result = StepResult(number, step.title, "skipped")
            elif report.failed_step is not None:
                result = StepResult(number, step.title, "not_run")
            else:
                result = _run_step(number, step)
            report.steps.append(result)
            if on_step is not None:
                on_step(result)
        report.finished_at = datetime.now(UTC)
        log.info("restore.finish", ok=report.ok, duration_s=report.duration_s)
    return report


def run(
    plan: RestorePlan,
    *,
    from_step: int = 1,
    on_step: Callable[[StepResult], None] | None = None,
    lock_path: Path | None = None,
    state_directory: Path | None = None,
) -> RestoreReport:
    """Execute ``plan`` under the run lock and save its report.

    The lock keeps scheduled backups (and prune) out while data is half restored: a backup
    taken then would record a state that never existed.
    """
    plan.check_from_step(from_step)
    with lock.acquire(lock_path):
        report = execute(plan, from_step=from_step, on_step=on_step)
    try:
        save_report(report, state_directory)
    except ConfigError as exc:
        report.errors.append(f"report: {exc.message}")
    return report


def save_report(report: RestoreReport, directory: Path | None = None) -> Path:
    """Write the report as JSON to ``<state dir>/reports/restore-<id>.json``.

    Sets ``report.report_path``, so the saved report names its own location.
    """
    target_dir = (directory or state.state_dir()) / REPORTS_DIR
    path = target_dir / f"restore-{report.restore_id}.json"
    report.report_path = path
    content = runner.redact(_to_json(report))
    try:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".restore.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        Path(tmp).replace(path)
    except OSError as exc:
        report.report_path = None
        raise ConfigError(
            f"cannot write restore report in {target_dir}: {exc.strerror}",
            hint=f"run drkit as root, or set ${state.STATE_DIR_ENV_VAR} to a writable directory",
        ) from None
    return path


def _run_step(number: int, step: Step) -> StepResult:
    log.info("restore.step", step=number, title=step.title)
    started = time.monotonic()
    detail: str | None = None
    error: str | None = None
    status: StepStatus = "failed"
    try:
        detail = step.action()
        status = "ok"
    except DrkitError as exc:
        error = runner.redact(exc.message + (f" (hint: {exc.hint})" if exc.hint else ""))
    except Exception as exc:
        # A bug must still end in a saved report that says where the restore stopped.
        log.exception("restore.step_crashed", step=number)
        error = runner.redact(f"unexpected error: {type(exc).__name__}: {exc}")
    result = StepResult(
        number=number,
        title=step.title,
        status=status,
        duration_s=round(time.monotonic() - started, 1),
        detail=runner.redact(detail) if detail else None,
        error=error,
    )
    log.info(
        "restore.step_finished",
        step=number,
        status=result.status,
        duration_s=result.duration_s,
        error=result.error,
    )
    return result


def _to_json(report: RestoreReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str) + "\n"
