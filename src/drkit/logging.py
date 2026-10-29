"""structlog setup: JSON lines for journald when not on a TTY, human-readable otherwise.

Logs go to stderr so stdout stays reserved for command output (``--json``). Every string in
every event passes through the runner's redactor before rendering.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from drkit.runner import redact


def configure(
    *, verbose: bool = False, json: bool | None = None, stream: TextIO | None = None
) -> None:
    """Configure process-wide logging. ``json=None`` picks JSON when ``stream`` is not a TTY."""
    out = stream if stream is not None else sys.stderr
    is_tty = out.isatty()
    use_json = (not is_tty) if json is None else json

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=is_tty)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_event,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
        logger_factory=structlog.WriteLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )


def redact_event(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact secrets from every value of the event."""
    return {key: _redact_value(value) for key, value in event_dict.items()}


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value
