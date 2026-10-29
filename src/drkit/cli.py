"""Command-line interface: option parsing, output rendering and exit codes."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from drkit import __version__, doctor
from drkit import logging as drkit_logging
from drkit.config import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    LoadedConfig,
    load_config,
    resolve_config_path,
)
from drkit.errors import DrkitError
from drkit.orchestrator import SUPPORTED_KINDS, restic_for, run_backup, run_prune
from drkit.runmanifest import RunManifest, list_runs, load_run
from drkit.runner import redact, redactor
from drkit.verify import DEFAULT_READ_DATA_SUBSET, READ_DATA_SUBSET_RE, run_verify

app = typer.Typer(
    name="drkit",
    help="Backup and disaster recovery for Docker Compose servers.",
    no_args_is_help=True,
    add_completion=False,
    # Locals in tracebacks may hold secrets (passwords, tokens): never render them.
    pretty_exceptions_show_locals=False,
)

console = Console()
err_console = Console(stderr=True)

_STATUS_STYLE = {
    doctor.Status.OK: "[green]✓[/green]",
    doctor.Status.WARN: "[yellow]![/yellow]",
    doctor.Status.FAIL: "[red]✗[/red]",
}


@dataclass(frozen=True, slots=True)
class Options:
    config: Path | None
    json: bool
    verbose: bool

    @property
    def config_path(self) -> Path:
        return resolve_config_path(self.config)


def handle_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn expected failures into an actionable message and their documented exit code."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except DrkitError as exc:
            err_console.print(f"[red]error:[/red] {escape(redact(exc.message))}")
            if exc.hint:
                err_console.print(f"[dim]hint:[/dim] {escape(redact(exc.hint))}")
            raise typer.Exit(int(exc.exit_code)) from exc

    return wrapper


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"drkit {__version__}", highlight=False)
        raise typer.Exit


@app.callback()
def _global_options(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            envvar=CONFIG_ENV_VAR,
            help=f"Manifest path (default: {DEFAULT_CONFIG_PATH}).",
            show_default=False,
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON output on stdout.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    drkit_logging.configure(verbose=verbose)
    ctx.obj = Options(config=config, json=json_output, verbose=verbose)


@app.command("doctor")
@handle_errors
def doctor_command(ctx: typer.Context) -> None:
    """Check the manifest, file permissions, dependencies and repository access."""
    options: Options = ctx.obj
    checks = doctor.run_checks(options.config_path)
    code = doctor.exit_code(checks)

    if options.json:
        payload = {"ok": code == 0, "checks": [c.to_dict() for c in checks]}
        typer.echo(redact(json.dumps(payload, indent=2)))
    else:
        _render_checks(checks)
    raise typer.Exit(int(code))


def _render_checks(checks: list[doctor.Check]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("", width=1)
    table.add_column("Category")
    table.add_column("Check", overflow="fold")
    table.add_column("Result", overflow="fold")
    for check in checks:
        table.add_row(
            _STATUS_STYLE[check.status],
            check.category.value,
            escape(check.name),
            escape(redact(check.detail)),
        )
    console.print(table)

    hints = [c for c in checks if c.status is not doctor.Status.OK and c.hint]
    if hints:
        console.print("\n[bold]How to fix[/bold]")
        for check in hints:
            console.print(f"  • {escape(check.name)}: {escape(redact(check.hint or ''))}")

    failed = sum(c.status is doctor.Status.FAIL for c in checks)
    if failed:
        console.print(f"\n[red]{failed} check(s) failed.[/red]")
    else:
        console.print("\n[green]All checks passed.[/green]")


def _load(options: Options) -> LoadedConfig:
    loaded = load_config(options.config_path)
    redactor.add_all(loaded.secret_values())
    return loaded


def _echo_json(payload: object) -> None:
    typer.echo(redact(json.dumps(payload, indent=2, default=str)))


@app.command("init")
@handle_errors
def init_command(ctx: typer.Context) -> None:
    """Initialize the restic repository (does nothing if it already exists)."""
    options: Options = ctx.obj
    loaded = _load(options)
    created = restic_for(loaded.manifest, dict(loaded.env)).init()
    url = redact(loaded.manifest.repository.url)
    if options.json:
        _echo_json({"repository": url, "created": created})
        return
    if not created:
        console.print(f"Repository {escape(url)} is already initialized.")
        return
    console.print(f"[green]Initialized restic repository[/green] {escape(url)}")
    console.print(
        "[yellow]Keep a copy of the password file and storage credentials outside this "
        "server: without them the backups cannot be restored.[/yellow]"
    )


@app.command("backup")
@handle_errors
def backup_command(
    ctx: typer.Context,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Back up a single project.")
    ] = None,
    only: Annotated[
        str | None,
        typer.Option(
            "--only",
            help=f"Comma-separated item kinds (available: {', '.join(SUPPORTED_KINDS)}).",
        ),
    ] = None,
) -> None:
    """Back up every item of the manifest as one run."""
    options: Options = ctx.obj
    loaded = _load(options)
    kinds = [kind.strip() for kind in only.split(",") if kind.strip()] if only else None
    report = run_backup(loaded, project=project, kinds=kinds)

    if options.json:
        _echo_json(
            {
                "run": report.manifest.model_dump(mode="json", by_alias=True),
                "retention": asdict(report.retention) if report.retention else None,
                "errors": report.errors,
                "notify_errors": report.notify_errors,
            }
        )
    else:
        _render_run(report.manifest)
        if report.retention and report.retention.removed_runs:
            console.print(
                f"Retention: dropped {len(report.retention.removed_runs)} run(s), "
                f"{report.retention.forgotten_snapshots} snapshot(s)."
            )
        for error in [*report.errors, *(f"notification {e}" for e in report.notify_errors)]:
            err_console.print(f"[yellow]warning:[/yellow] {escape(redact(error))}")
    raise typer.Exit(int(report.exit_code))


def _read_data_subset(value: str) -> str:
    if value != "0" and not READ_DATA_SUBSET_RE.match(value):
        raise typer.BadParameter(
            "use a percentage (5%), a fraction (1/7) or a size (2G); 0 skips it"
        )
    return value


@app.command("verify")
@handle_errors
def verify_command(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option("--run", help="Run ID, or 'latest'.")] = "latest",
    read_data_subset: Annotated[
        str,
        typer.Option(
            "--read-data-subset",
            callback=_read_data_subset,
            help="Share of the repository data restic reads back and checks (0: structure only).",
        ),
    ] = DEFAULT_READ_DATA_SUBSET,
) -> None:
    """Check repository integrity and restore every database of a run into a throwaway container."""
    options: Options = ctx.obj
    loaded = _load(options)
    report = run_verify(
        loaded,
        run_id=run_id,
        read_data_subset=None if read_data_subset == "0" else read_data_subset,
    )

    if options.json:
        _echo_json(
            {
                "run_id": report.run_id,
                "ok": report.ok,
                "repository_error": report.repository_error,
                "databases": [asdict(db) for db in report.databases],
                "errors": report.errors,
                "notify_errors": report.notify_errors,
            }
        )
    else:
        console.print(f"Verify of run [bold]{report.run_id}[/bold]")
        if report.repository_error is None:
            console.print(f"{_STATUS_STYLE[doctor.Status.OK]} repository check")
        else:
            console.print(
                f"{_STATUS_STYLE[doctor.Status.FAIL]} repository check: "
                f"{escape(redact(report.repository_error))}"
            )
        table = Table(box=None, pad_edge=False, header_style="bold")
        for column in ("", "Database", "Image", "Tables", "Time", "Error"):
            table.add_column(column, overflow="fold")
        for db in report.databases:
            table.add_row(
                _STATUS_STYLE[doctor.Status.OK if db.status == "ok" else doctor.Status.FAIL],
                escape(db.label),
                escape(db.image or ""),
                "" if db.tables is None else str(db.tables),
                f"{db.duration_s:.1f}s",
                escape(redact(db.error or "")),
            )
        if report.databases:
            console.print(table)
        else:
            console.print("No database in this run.")
        for error in [*report.errors, *(f"notification {e}" for e in report.notify_errors)]:
            err_console.print(f"[yellow]warning:[/yellow] {escape(redact(error))}")
    raise typer.Exit(int(report.exit_code))


@app.command("prune")
@handle_errors
def prune_command(ctx: typer.Context) -> None:
    """Forget snapshots of runs that never finished, then delete unreferenced data."""
    options: Options = ctx.obj
    report = run_prune(_load(options))
    if options.json:
        _echo_json(asdict(report))
        return
    if report.orphan_runs:
        console.print(
            f"Forgot {report.forgotten_snapshots} snapshot(s) of "
            f"{len(report.orphan_runs)} incomplete run(s): {', '.join(report.orphan_runs)}"
        )
    console.print("[green]Repository pruned.[/green]")


runs_app = typer.Typer(help="Inspect backup runs.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")


@runs_app.command("list")
@handle_errors
def runs_list_command(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Number of runs.")] = 20,
) -> None:
    """List the most recent runs, newest first."""
    options: Options = ctx.obj
    loaded = _load(options)
    runs = list(reversed(list_runs(restic_for(loaded.manifest, dict(loaded.env)))))[:limit]
    if options.json:
        _echo_json([asdict(run) for run in runs])
        return
    if not runs:
        console.print("No runs yet. Create one with `drkit backup`.")
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("Run", "Time (UTC)", "Status"):
        table.add_column(column)
    for run in runs:
        table.add_row(
            run.run_id, f"{run.time.astimezone(UTC):%Y-%m-%d %H:%M:%S}", _status(run.status)
        )
    console.print(table)


@runs_app.command("show")
@handle_errors
def runs_show_command(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID, or 'latest'.")] = "latest",
) -> None:
    """Show the run manifest of a run."""
    options: Options = ctx.obj
    loaded = _load(options)
    manifest = load_run(restic_for(loaded.manifest, dict(loaded.env)), run_id)
    if options.json:
        typer.echo(redact(manifest.to_json()), nl=False)
        return
    _render_run(manifest)


def _render_run(run: RunManifest) -> None:
    duration = (run.finished_at - run.started_at).total_seconds()
    console.print(
        f"Run [bold]{run.run_id}[/bold] on {escape(run.host)}: {_status(run.status)} "
        f"({duration:.0f}s, drkit {run.drkit_version})"
    )
    if not run.items:
        console.print("No items.")
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("", "Item", "Engine", "Size", "Time", "Snapshot / error"):
        table.add_column(column, overflow="fold")
    for item in run.items:
        engine = f"{item.engine} {item.engine_version or ''}".strip() if item.engine else ""
        outcome = (item.snapshot or "")[:8] if item.status == "ok" else redact(item.error or "")
        table.add_row(
            _STATUS_STYLE[doctor.Status.OK if item.status == "ok" else doctor.Status.FAIL],
            escape(item.label),
            escape(engine),
            _human_bytes(item.bytes) if item.status == "ok" else "",
            f"{item.duration_s:.1f}s",
            escape(outcome),
        )
    console.print(table)
    for name, project in run.projects.items():
        commit = (project.sha or "?")[:12] + (" [yellow](dirty)[/yellow]" if project.dirty else "")
        console.print(
            f"{escape(name)}: git {commit}, {len(project.images)} image(s) pinned", highlight=False
        )


def _status(status: str) -> str:
    color = {"ok": "green", "partial": "yellow"}.get(status, "red")
    return f"[{color}]{escape(status)}[/{color}]"


def _human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def main() -> None:
    app()
