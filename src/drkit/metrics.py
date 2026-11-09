"""Prometheus metrics in the text exposition format, for node_exporter's textfile collector.

Generated from ``state.json`` only: writing them never opens the repository, so they can be
refreshed after every command, and read by ``drkit metrics`` at any time. A metric without
data (no run yet, a state file of 1.0) is left out, never exported as a false zero.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import structlog

from drkit import __version__, state
from drkit.config import Manifest
from drkit.errors import DrkitError

FILE_MODE = 0o644  # node_exporter usually runs as its own user

Labels = dict[str, str]

log = structlog.get_logger(__name__)


@dataclass
class Family:
    name: str
    help: str
    samples: list[tuple[Labels, float]] = field(default_factory=list)

    def add(self, value: float | None, **labels: str) -> None:
        if value is not None:
            self.samples.append((labels, value))


def collect(current: state.State, host: str) -> list[Family]:
    """The metric families of ``current``, each sample labelled with ``host``."""

    def family(name: str, help_text: str) -> Family:
        families.append(Family(name, help_text))
        return families[-1]

    def add(target: Family, value: float | None, **labels: str) -> None:
        target.add(value, host=host, **labels)

    families: list[Family] = []
    add(family("drkit_info", "drkit version, always 1."), 1, version=__version__)

    run, success = current.last_run, current.last_success
    add(
        family("drkit_last_run_timestamp_seconds", "End of the last backup run."),
        _seconds(run.finished_at) if run else None,
    )
    add(
        family(
            "drkit_last_success_timestamp_seconds",
            "End of the last backup run in which every item succeeded.",
        ),
        _seconds(success.finished_at) if success else None,
    )
    add(
        family("drkit_last_run_duration_seconds", "Duration of the last backup run."),
        (run.finished_at - run.started_at).total_seconds() if run else None,
    )
    add(
        family("drkit_last_run_success", "1 if every item of the last backup run succeeded."),
        _flag(run.status == "ok") if run else None,
    )
    items = family("drkit_last_run_items", "Items of the last backup run, by outcome.")
    if run is not None:
        add(items, run.items_ok, status="ok")
        add(items, run.items_failed, status="failed")
    add(
        family(
            "drkit_last_run_bytes_added",
            "New data the last backup run added to the repository (deduplicated).",
        ),
        run.bytes_added if run else None,
    )
    projects = family(
        "drkit_project_last_success_timestamp_seconds",
        "End of the last backup run in which every item of the project succeeded.",
    )
    for name, project in sorted(current.projects.items()):
        add(projects, _seconds(project.last_success_at), project=name)

    verify = current.last_verify
    add(
        family("drkit_last_verify_timestamp_seconds", "End of the last drkit verify."),
        _seconds(verify.finished_at) if verify else None,
    )
    add(
        family("drkit_last_verify_success", "1 if the last drkit verify passed."),
        _flag(verify.status == "ok") if verify else None,
    )

    prune = current.last_prune
    add(
        family("drkit_last_prune_timestamp_seconds", "End of the last drkit prune."),
        _seconds(prune.finished_at) if prune else None,
    )
    add(
        family("drkit_last_prune_success", "1 if the last drkit prune succeeded."),
        _flag(prune.status == "ok") if prune else None,
    )

    restored_at = family(
        "drkit_last_restore_timestamp_seconds", "End of the last restore, by kind."
    )
    restored_ok = family("drkit_last_restore_success", "1 if the last restore succeeded, by kind.")
    restore_rto = family(
        "drkit_last_restore_rto_seconds",
        "Recovery time of the last successful restore, by kind (host: start to the last "
        "green healthcheck; db and project: duration).",
    )
    for kind, restore in sorted(current.last_restores.items()):
        add(restored_at, _seconds(restore.finished_at), kind=kind)
        add(restored_ok, _flag(restore.status == "ok"), kind=kind)
        add(restore_rto, restore.rto_s, kind=kind)

    drill = current.last_drill
    add(
        family("drkit_last_drill_timestamp_seconds", "End of the last DR drill."),
        _seconds(drill.finished_at) if drill else None,
    )
    add(
        family("drkit_last_drill_success", "1 if the last DR drill passed."),
        _flag(drill.status == "ok") if drill else None,
    )
    add(
        family("drkit_last_drill_rto_seconds", "RTO measured by the last DR drill."),
        drill.rto_s if drill else None,
    )
    add(
        family("drkit_last_drill_rpo_seconds", "RPO measured by the last DR drill."),
        drill.rpo_s if drill else None,
    )
    return [f for f in families if f.samples]


def render(families: Iterable[Family]) -> str:
    """The text exposition format: ``# HELP``, ``# TYPE`` and the samples of each family."""
    lines: list[str] = []
    for metric in families:
        lines.append(f"# HELP {metric.name} {_escape_help(metric.help)}")
        lines.append(f"# TYPE {metric.name} gauge")
        for labels, value in metric.samples:
            pairs = ",".join(f'{key}="{escape_label(val)}"' for key, val in labels.items())
            lines.append(f"{metric.name}{{{pairs}}} {format_value(value)}")
    return "\n".join(lines) + "\n"


def generate(host: str, directory: Path | None = None) -> str:
    """The metrics of the state file in ``directory`` (``$DRKIT_STATE_DIR`` by default)."""
    return render(collect(state.load(directory), host))


def write(path: Path, content: str) -> None:
    """Replace ``path`` atomically: node_exporter never reads a partial file.

    The temporary file is in the same directory (so the rename is atomic) and its name does
    not end in ``.prom`` (so the collector ignores it).
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).chmod(FILE_MODE)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def refresh(manifest: Manifest, directory: Path | None = None) -> str | None:
    """Rewrite ``metrics.textfile`` if configured. Never raises: metrics must not change the
    outcome of the command that refreshes them. Returns the error, if any."""
    if manifest.metrics is None:
        return None
    path = manifest.metrics.textfile
    try:
        write(path, generate(manifest.host, directory))
    except (OSError, DrkitError) as exc:
        error = exc.message if isinstance(exc, DrkitError) else (exc.strerror or str(exc))
        log.warning("metrics.not_written", path=str(path), error=error)
        return f"cannot write metrics to {path}: {error}"
    log.debug("metrics.written", path=str(path))
    return None


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _seconds(moment: datetime) -> float:
    return moment.timestamp()


def _flag(condition: bool) -> int:
    return 1 if condition else 0
