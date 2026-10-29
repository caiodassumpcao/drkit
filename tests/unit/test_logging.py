import io
import json
from collections.abc import Iterator

import pytest
import structlog

from drkit import logging as drkit_logging
from drkit import runner
from drkit.runner import REDACTED


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    runner.redactor.clear()
    yield
    runner.redactor.clear()
    structlog.reset_defaults()


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_output_with_level_and_timestamp() -> None:
    stream = io.StringIO()
    drkit_logging.configure(json=True, stream=stream)
    structlog.get_logger().info("backup.item", run_id="r1", duration_s=1.5)

    [event] = _lines(stream)
    assert event["event"] == "backup.item"
    assert event["level"] == "info"
    assert event["run_id"] == "r1"
    assert event["duration_s"] == 1.5
    assert "timestamp" in event


def test_defaults_to_json_when_not_a_tty() -> None:
    stream = io.StringIO()
    drkit_logging.configure(stream=stream)
    structlog.get_logger().info("hello")
    assert _lines(stream)[0]["event"] == "hello"


def test_console_output_when_requested() -> None:
    stream = io.StringIO()
    drkit_logging.configure(json=False, stream=stream)
    structlog.get_logger().info("hello", item="db")
    output = stream.getvalue()
    assert "hello" in output
    assert "item" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_debug_is_filtered_unless_verbose() -> None:
    quiet = io.StringIO()
    drkit_logging.configure(json=True, stream=quiet)
    structlog.get_logger().debug("noise")
    assert quiet.getvalue() == ""

    loud = io.StringIO()
    drkit_logging.configure(json=True, stream=loud, verbose=True)
    structlog.get_logger().debug("noise")
    assert _lines(loud)[0]["event"] == "noise"


def test_secrets_are_redacted_everywhere() -> None:
    runner.redactor.add("hunter2-hunter2")
    stream = io.StringIO()
    drkit_logging.configure(json=True, stream=stream)
    structlog.get_logger().info(
        "failed hunter2-hunter2",
        detail="pw hunter2-hunter2",
        nested={"cmd": ["mysql", "hunter2-hunter2"], "n": 3},
        args=("hunter2-hunter2",),
    )
    assert "hunter2-hunter2" not in stream.getvalue()
    [event] = _lines(stream)
    assert event["event"] == f"failed {REDACTED}"
    assert event["nested"] == {"cmd": ["mysql", REDACTED], "n": 3}


def test_exception_tracebacks_are_redacted() -> None:
    runner.redactor.add("hunter2-hunter2")
    stream = io.StringIO()
    drkit_logging.configure(json=True, stream=stream)
    try:
        raise RuntimeError("boom hunter2-hunter2")
    except RuntimeError:
        structlog.get_logger().exception("crash")
    assert "hunter2-hunter2" not in stream.getvalue()
    assert "RuntimeError" in stream.getvalue()
