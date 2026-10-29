"""The only way drkit runs external commands.

Guarantees for every call: an argument list (never a shell), a mandatory timeout, and secret
redaction of everything that may end up in logs, error messages or reports.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import structlog

from drkit.errors import CommandError, CommandTimeoutError, MissingDependencyError

REDACTED = "***"

# Shorter values are not registered as secrets: redacting them would mangle unrelated output
# (e.g. a "1" or "db") far more often than it would protect anything.
MIN_SECRET_LENGTH = 6

# Captured streams are bounded so a chatty command cannot exhaust memory; the tail is what
# matters for diagnosing failures.
MAX_CAPTURE_BYTES = 1024 * 1024

_SECRET_WORDS = "PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY"  # noqa: S105
_SECRET_KEY = rf"[A-Za-z0-9_.-]*(?:{_SECRET_WORDS})[A-Za-z0-9_.-]*"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # KEY=value, --key=value, key: value
    (
        re.compile(rf"(?i)\b({_SECRET_KEY})(\s*[=:]\s*)(?!{re.escape(REDACTED)})(\S+)"),
        rf"\1\2{REDACTED}",
    ),
    # scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)"), rf"\1{REDACTED}\3"),
    # Telegram bot tokens (also when embedded in api.telegram.org/bot<token>/ URLs)
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), REDACTED),
)

log = structlog.get_logger(__name__)


class Redactor:
    """Replaces registered secret values and well-known secret patterns with ``***``."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, *values: str | None) -> None:
        """Register secret values. ``None`` and values under MIN_SECRET_LENGTH are ignored."""
        with self._lock:
            self._secrets.update(
                v for v in values if v is not None and len(v.strip()) >= MIN_SECRET_LENGTH
            )

    def add_all(self, values: Iterable[str]) -> None:
        self.add(*values)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def redact(self, text: str) -> str:
        with self._lock:
            # Longest first, so a secret containing another secret is replaced as a whole.
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


redactor = Redactor()
"""Process-wide redactor. Secrets from the manifest and env file are registered on load."""


def redact(text: str) -> str:
    return redactor.redact(text)


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        """Redacted, shell-quoted command line, safe to log."""
        return format_command(self.args)


@dataclass(frozen=True, slots=True)
class PipeResult:
    """Outcome of ``producer | consumer``. Both exit codes must be checked by the caller."""

    producer: CommandResult
    consumer: CommandResult

    @property
    def ok(self) -> bool:
        return self.producer.ok and self.consumer.ok


def format_command(args: Sequence[str]) -> str:
    return redact(shlex.join(args))


def run(
    args: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    input: str | None = None,
    check: bool = True,
) -> CommandResult:
    """Run a command and capture its output.

    Values passed in ``env`` are added on top of the current environment and treated as secrets.
    Raises MissingDependencyError if the binary is not found, CommandTimeoutError on timeout and,
    when ``check`` is true, CommandError on a non-zero exit code.
    """
    argv = _argv(args)
    log.debug("command.start", command=format_command(argv))
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv,
            input=input.encode() if input is not None else None,
            capture_output=True,
            timeout=timeout,
            env=_environment(env),
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _not_found(argv[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise _timeout(format_command(argv), timeout) from exc

    result = CommandResult(
        args=argv,
        returncode=proc.returncode,
        stdout=_decode(proc.stdout[-MAX_CAPTURE_BYTES:]),
        stderr=_decode(proc.stderr[-MAX_CAPTURE_BYTES:]),
        duration_s=time.monotonic() - started,
    )
    _log_finish(result)
    if check and not result.ok:
        raise _failed(result)
    return result


def pipe(
    producer: Sequence[str],
    consumer: Sequence[str],
    *,
    timeout: float,
    producer_env: Mapping[str, str] | None = None,
    consumer_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> PipeResult:
    """Stream ``producer``'s stdout into ``consumer``'s stdin without touching the disk.

    Never raises on non-zero exit codes: a pipeline where only one side fails is a distinct,
    meaningful outcome (e.g. a failed dump that restic still stored), so the caller decides.
    Raises MissingDependencyError if a binary is not found and CommandTimeoutError if the whole
    pipeline exceeds ``timeout`` (both processes are killed).
    """
    p_argv, c_argv = _argv(producer), _argv(consumer)
    log.debug("pipe.start", producer=format_command(p_argv), consumer=format_command(c_argv))
    started = time.monotonic()
    deadline = started + timeout

    try:
        p_proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell
            p_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(producer_env),
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise _not_found(p_argv[0]) from exc
    assert p_proc.stdout is not None  # noqa: S101 - guaranteed by stdout=PIPE
    try:
        c_proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell
            c_argv,
            stdin=p_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(consumer_env),
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        _kill(p_proc)
        raise _not_found(c_argv[0]) from exc
    # Drop our copy of the pipe so the producer gets SIGPIPE if the consumer exits early.
    p_proc.stdout.close()

    buffers = {"p_err": bytearray(), "c_out": bytearray(), "c_err": bytearray()}
    readers = [
        _start_reader(stream, buffers[key])
        for stream, key in (
            (p_proc.stderr, "p_err"),
            (c_proc.stdout, "c_out"),
            (c_proc.stderr, "c_err"),
        )
    ]

    durations: dict[str, float] = {}
    try:
        for key, proc in (("producer", p_proc), ("consumer", c_proc)):
            proc.wait(timeout=max(deadline - time.monotonic(), 0))
            durations[key] = time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        _kill(p_proc)
        _kill(c_proc)
        for reader in readers:
            reader.join()
        raise _timeout(f"{format_command(p_argv)} | {format_command(c_argv)}", timeout) from exc
    for reader in readers:
        reader.join()

    result = PipeResult(
        producer=CommandResult(
            args=p_argv,
            returncode=p_proc.returncode,
            stdout="",
            stderr=_decode(bytes(buffers["p_err"])),
            duration_s=durations["producer"],
        ),
        consumer=CommandResult(
            args=c_argv,
            returncode=c_proc.returncode,
            stdout=_decode(bytes(buffers["c_out"])),
            stderr=_decode(bytes(buffers["c_err"])),
            duration_s=durations["consumer"],
        ),
    )
    _log_finish(result.producer)
    _log_finish(result.consumer)
    return result


def failure_detail(result: CommandResult, *, max_lines: int = 5) -> str:
    """Last lines of stderr (or stdout), redacted: enough context for an error message."""
    output = (result.stderr or result.stdout).strip()
    tail = "\n".join(output.splitlines()[-max_lines:])
    return redact(tail)


def _argv(args: Sequence[str]) -> tuple[str, ...]:
    if isinstance(args, str) or not args:
        raise ValueError("command must be a non-empty sequence of arguments")
    return tuple(args)


def _environment(extra: Mapping[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return None
    redactor.add_all(extra.values())
    return {**os.environ, **extra}


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _start_reader(stream: IO[bytes] | None, sink: bytearray) -> threading.Thread:
    def drain() -> None:
        if stream is None:
            return
        with stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                sink.extend(chunk)
                if len(sink) > MAX_CAPTURE_BYTES:
                    del sink[: len(sink) - MAX_CAPTURE_BYTES]

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return thread


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _log_finish(result: CommandResult) -> None:
    log.debug(
        "command.finish",
        command=result.command,
        returncode=result.returncode,
        duration_s=round(result.duration_s, 3),
    )


def _not_found(binary: str) -> MissingDependencyError:
    return MissingDependencyError(
        f"command not found: {binary}",
        hint=f"install {binary} and make sure it is in PATH (run `drkit doctor` for details)",
    )


def _timeout(command: str, timeout: float) -> CommandTimeoutError:
    return CommandTimeoutError(
        f"command timed out after {timeout:g}s and was killed: {command}",
        hint="check that the target service is responsive, or raise the timeout",
    )


def _failed(result: CommandResult) -> CommandError:
    detail = failure_detail(result)
    message = f"command failed with exit code {result.returncode}: {result.command}"
    return CommandError(f"{message}\n{detail}" if detail else message)
