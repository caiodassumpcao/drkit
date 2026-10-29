"""Command-line interface: option parsing, output rendering and exit codes."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from drkit import __version__, doctor
from drkit import logging as drkit_logging
from drkit.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, resolve_config_path
from drkit.errors import DrkitError
from drkit.runner import redact

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
    """Check dependencies, file permissions and the manifest."""
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


def main() -> None:
    app()
