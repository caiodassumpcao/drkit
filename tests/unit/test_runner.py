from collections.abc import Iterator
from pathlib import Path

import pytest

from drkit import runner
from drkit.errors import CommandError, CommandTimeoutError, MissingDependencyError
from drkit.runner import REDACTED, Redactor


@pytest.fixture(autouse=True)
def _clean_global_redactor() -> Iterator[None]:
    runner.redactor.clear()
    yield
    runner.redactor.clear()


class TestRedactor:
    def test_registered_values_are_replaced(self) -> None:
        r = Redactor()
        r.add("s3cr3t-value", None)
        assert r.redact("pass is s3cr3t-value!") == f"pass is {REDACTED}!"

    def test_short_values_are_not_registered(self) -> None:
        r = Redactor()
        r.add("db", "  abc  ")
        assert r.redact("db abc") == "db abc"

    def test_longest_secret_wins(self) -> None:
        r = Redactor()
        r.add_all(["abcdef", "abcdefghij"])
        assert r.redact("x abcdefghij y") == f"x {REDACTED} y"

    def test_clear(self) -> None:
        r = Redactor()
        r.add("s3cr3t-value")
        r.clear()
        assert r.redact("s3cr3t-value") == "s3cr3t-value"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("AWS_SECRET_ACCESS_KEY=abc/def+1", f"AWS_SECRET_ACCESS_KEY={REDACTED}"),
            ("MYSQL_PWD=hunter2 mysqldump", f"MYSQL_PWD={REDACTED} mysqldump"),
            ("--password=hunter2", f"--password={REDACTED}"),
            ("api_key: 123456", f"api_key: {REDACTED}"),
            ("db_password = x", f"db_password = {REDACTED}"),
            ("s3:https://user:pa55@minio:9000/b", f"s3:https://user:{REDACTED}@minio:9000/b"),
            (
                "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1/send",
                f"https://api.telegram.org/bot{REDACTED}/send",
            ),
            ("nothing to see here", "nothing to see here"),
        ],
    )
    def test_known_patterns(self, text: str, expected: str) -> None:
        assert Redactor().redact(text) == expected

    def test_pattern_is_idempotent(self) -> None:
        once = Redactor().redact("TOKEN=abcdef")
        assert Redactor().redact(once) == once


class TestRun:
    def test_captures_output(self) -> None:
        result = runner.run(["sh", "-c", "echo out; echo err >&2"], timeout=5)
        assert result.ok
        assert result.stdout == "out\n"
        assert result.stderr == "err\n"
        assert result.duration_s >= 0

    def test_passes_input_cwd_and_env(self, tmp_path: Path) -> None:
        result = runner.run(
            ["sh", "-c", 'cat; pwd; printf "%s" "$DRKIT_TEST"'],
            timeout=5,
            input="in\n",
            cwd=tmp_path,
            env={"DRKIT_TEST": "value"},
        )
        assert result.stdout == f"in\n{tmp_path}\nvalue"

    def test_env_values_are_registered_as_secrets(self) -> None:
        runner.run(["true"], timeout=5, env={"X": "super-secret"})
        assert runner.redact("super-secret") == REDACTED

    def test_non_zero_exit_raises_with_redacted_detail(self) -> None:
        runner.redactor.add("leaked-secret")
        with pytest.raises(CommandError) as excinfo:
            runner.run(["sh", "-c", "echo leaked-secret >&2; exit 3"], timeout=5)
        message = str(excinfo.value)
        assert "exit code 3" in message
        assert "leaked-secret" not in message
        assert REDACTED in message

    def test_non_zero_exit_without_check(self) -> None:
        result = runner.run(["false"], timeout=5, check=False)
        assert not result.ok
        assert result.returncode == 1

    def test_missing_binary(self) -> None:
        with pytest.raises(MissingDependencyError, match="drkit-no-such-binary"):
            runner.run(["drkit-no-such-binary"], timeout=5)

    def test_timeout_kills_command(self) -> None:
        with pytest.raises(CommandTimeoutError, match=r"timed out after 0\.2s"):
            runner.run(["sleep", "5"], timeout=0.2)

    @pytest.mark.parametrize("args", [[], "echo hi"])
    def test_rejects_empty_or_string_commands(self, args: list[str] | str) -> None:
        with pytest.raises(ValueError, match="non-empty sequence"):
            runner.run(args, timeout=5)

    def test_command_property_is_redacted(self) -> None:
        result = runner.run(["echo", "TOKEN=abcdef"], timeout=5)
        assert result.command == f"echo TOKEN={REDACTED}"


class TestPipe:
    def test_streams_producer_into_consumer(self) -> None:
        result = runner.pipe(["printf", "a\\nb\\nc\\n"], ["wc", "-l"], timeout=5)
        assert result.ok
        assert result.consumer.stdout.strip() == "3"

    def test_streams_large_payload(self) -> None:
        # Larger than any pipe buffer: proves neither side deadlocks.
        result = runner.pipe(["head", "-c", "5000000", "/dev/zero"], ["wc", "-c"], timeout=10)
        assert result.ok
        assert result.consumer.stdout.strip() == "5000000"

    def test_producer_failure_is_reported_even_if_consumer_succeeds(self) -> None:
        result = runner.pipe(
            ["sh", "-c", "echo partial; echo 'dump failed' >&2; exit 2"],
            ["cat"],
            timeout=5,
        )
        assert not result.ok
        assert result.producer.returncode == 2
        assert result.producer.stderr == "dump failed\n"
        assert result.consumer.ok
        assert result.consumer.stdout == "partial\n"

    def test_consumer_failure_is_reported(self) -> None:
        result = runner.pipe(["echo", "x"], ["sh", "-c", "cat >/dev/null; exit 4"], timeout=5)
        assert result.producer.ok
        assert result.consumer.returncode == 4

    def test_producer_gets_sigpipe_when_consumer_exits_early(self) -> None:
        result = runner.pipe(["yes"], ["head", "-n", "1"], timeout=5)
        assert result.consumer.ok
        assert result.producer.returncode != 0

    def test_timeout_kills_both(self) -> None:
        with pytest.raises(CommandTimeoutError, match=r"sleep 5 \| cat"):
            runner.pipe(["sleep", "5"], ["cat"], timeout=0.2)

    def test_missing_producer(self) -> None:
        with pytest.raises(MissingDependencyError):
            runner.pipe(["drkit-no-such-binary"], ["cat"], timeout=5)

    def test_missing_consumer(self) -> None:
        with pytest.raises(MissingDependencyError):
            runner.pipe(["yes"], ["drkit-no-such-binary"], timeout=5)

    def test_env_is_applied_per_side(self) -> None:
        result = runner.pipe(
            ["sh", "-c", 'printf "%s" "$A"'],
            ["sh", "-c", 'cat; printf "%s" "${A:-unset}"'],
            timeout=5,
            producer_env={"A": "from-producer"},
        )
        assert result.consumer.stdout == "from-producerunset"


def test_failure_detail_uses_last_lines() -> None:
    result = runner.run(["sh", "-c", "seq 1 10 >&2; exit 1"], timeout=5, check=False)
    assert runner.failure_detail(result, max_lines=2) == "9\n10"
