"""``drkit report`` (DESIGN §10): recent runs, current RPO and the last verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drkit import state
from drkit.restic import Restic
from drkit.runmanifest import list_runs, load_manifest

DEFAULT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class RunLine:
    run_id: str
    status: str
    finished_at: datetime
    items: int
    failed: int
    bytes: int
    duration_s: float


@dataclass(frozen=True, slots=True)
class StatusReport:
    host: str
    generated_at: datetime
    runs: list[RunLine]
    """Most recent first."""
    last_success_run: str | None
    last_success_at: datetime | None
    last_verify: state.VerifyRecord | None

    @property
    def rpo_s(self) -> float | None:
        """Data lost if the host died now: time since the last successful run."""
        if self.last_success_at is None:
            return None
        return (self.generated_at - self.last_success_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rpo_s"] = self.rpo_s
        data["last_verify"] = self.last_verify.model_dump(mode="json") if self.last_verify else None
        data["last_drill"] = None  # recorded from v1.0 (drkit drill)
        return data


def build_report(
    restic: Restic,
    *,
    limit: int = DEFAULT_LIMIT,
    state_directory: Path | None = None,
    now: datetime | None = None,
) -> StatusReport:
    """The repository is the source of truth for runs; ``state.json`` adds the last verify."""
    summaries = list_runs(restic)
    lines = []
    for summary in reversed(summaries[-limit:]):
        run = load_manifest(restic, summary)
        lines.append(
            RunLine(
                run_id=run.run_id,
                status=run.status,
                finished_at=run.finished_at,
                items=len(run.items),
                failed=len(run.failed_items),
                bytes=sum(item.bytes for item in run.items),
                duration_s=round((run.finished_at - run.started_at).total_seconds(), 1),
            )
        )
    # The meta snapshot is the last write of a run: its time is when the run became restorable.
    last_ok = next((s for s in reversed(summaries) if s.status == "ok"), None)
    return StatusReport(
        host=restic.host,
        generated_at=now or datetime.now(UTC),
        runs=lines,
        last_success_run=last_ok.run_id if last_ok else None,
        last_success_at=last_ok.time if last_ok else None,
        last_verify=state.load(state_directory).last_verify,
    )


def to_markdown(report: StatusReport) -> str:
    rpo = (
        f"{human_duration(report.rpo_s)} (last successful run `{report.last_success_run}`)"
        if report.rpo_s is not None
        else "unknown: no successful run"
    )
    verify = (
        f"{report.last_verify.status} · run `{report.last_verify.run_id}` · "
        f"{_utc(report.last_verify.finished_at)}"
        if report.last_verify
        else "never"
    )
    lines = [
        f"# drkit report: {report.host}",
        "",
        f"Generated {_utc(report.generated_at)}.",
        "",
        "| | |",
        "|---|---|",
        f"| Current RPO | {rpo} |",
        f"| Last verify | {verify} |",
        "| Last drill | never |",
        "",
        f"## Last {len(report.runs)} run(s)",
        "",
    ]
    if not report.runs:
        return "\n".join([*lines, "No runs yet."]) + "\n"
    lines += [
        "| Run | Finished | Status | Items | Failed | Size | Duration |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    lines += [
        f"| `{run.run_id}` | {_utc(run.finished_at)} | {run.status} | {run.items} | "
        f"{run.failed} | {human_bytes(run.bytes)} | {human_duration(run.duration_s)} |"
        for run in report.runs
    ]
    return "\n".join(lines) + "\n"


def human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def human_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _utc(moment: datetime) -> str:
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
