import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from drkit import restic as restic_module
from drkit import runner
from drkit.config import Repository, Retention
from drkit.errors import CommandError, CommandTimeoutError, ConfigError
from drkit.restic import Restic, Snapshot
from drkit.runner import CommandResult

REPO = Repository(
    url="s3:https://user:hunter22@s3.example.com/bucket", password_file=Path("/etc/drkit/pw")
)


def snapshot_json(
    sid: str, tags: Sequence[str], time: str = "2026-10-29T03:00:00Z", paths: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "id": sid * 8,
        "short_id": sid,
        "time": time,
        "hostname": "prod-01",
        "tags": list(tags),
        "paths": list(paths),
    }


SUMMARY = json.dumps(
    {
        "message_type": "summary",
        "snapshot_id": "abc123",
        "total_bytes_processed": 42,
        "data_added": 7,
    }
)


@dataclass
class FakeRun:
    """Records restic invocations and answers from a queue of (exit code, stdout, stderr)."""

    responses: list[tuple[int, str, str]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"args": list(args), **kwargs})
        code, out, err = self.responses.pop(0) if self.responses else (0, "", "")
        return CommandResult(tuple(args), code, out, err, 0.0)

    @property
    def argv(self) -> list[list[str]]:
        return [c["args"] for c in self.calls]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(runner, "run", fake)
    return fake


@pytest.fixture
def repo() -> Restic:
    return Restic(REPO, host="prod-01", env={"AWS_SECRET_ACCESS_KEY": "aws-secret"})


def test_environment_points_restic_at_repository(repo: Restic, fake: FakeRun) -> None:
    repo.exists()
    env = fake.calls[0]["env"]
    assert env == {
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "RESTIC_REPOSITORY": REPO.url,
        "RESTIC_PASSWORD_FILE": "/etc/drkit/pw",
    }
    assert fake.calls[0]["check"] is False


class TestRepository:
    def test_exists(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(0, "{}", ""), (10, "", "repository does not exist")]
        assert repo.exists() is True
        assert repo.exists() is False
        assert fake.argv[0] == ["restic", "cat", "config"]

    def test_init(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(0, "", "")]
        assert repo.init() is True
        assert fake.argv == [["restic", "init"]]

    def test_init_when_already_initialized(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [
            (1, "", "Fatal: create key in repository at s3:... failed: repository master key "
             "and config already initialized"),
        ]  # fmt: skip
        assert repo.init() is False

    def test_init_failure(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(1, "", "Fatal: create repository failed: Access Denied")]
        with pytest.raises(
            CommandError, match=r"restic init failed with exit code 1: .*Access Denied"
        ):
            repo.init()

    def test_exists_times_out_with_an_explanation(
        self, repo: Restic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timeouts: list[float] = []

        def hang(args: Sequence[str], **kwargs: Any) -> CommandResult:
            timeouts.append(kwargs["timeout"])
            raise CommandTimeoutError("timed out")

        monkeypatch.setattr(runner, "run", hang)
        with pytest.raises(CommandError, match="did not answer within 60s") as excinfo:
            repo.exists()
        assert timeouts == [60.0]
        assert excinfo.value.hint is not None
        assert "bucket exists" in excinfo.value.hint
        assert "hunter22" not in excinfo.value.message

    def test_ensure_ready(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(10, "", "")]
        with pytest.raises(ConfigError, match="is not initialized") as excinfo:
            repo.ensure_ready()
        assert excinfo.value.hint == "run `drkit init`"
        assert "hunter22" not in excinfo.value.message
        repo.ensure_ready()  # default response: exit 0

    @pytest.mark.parametrize(
        ("code", "error", "message"),
        [
            (12, ConfigError, "wrong password"),
            (11, CommandError, "is locked"),
            (1, CommandError, "restic cat failed with exit code 1: Fatal: unable to open"),
        ],
    )
    def test_access_errors(
        self, repo: Restic, fake: FakeRun, code: int, error: type[Exception], message: str
    ) -> None:
        fake.responses = [(code, "", "Fatal: unable to open")]
        with pytest.raises(error, match=message):
            repo.exists()

    def test_missing_repository_during_a_command(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(10, "", "")]
        with pytest.raises(ConfigError, match="does not exist"):
            repo.snapshots()


class TestBackup:
    def test_stdin_command(self, repo: Restic) -> None:
        assert repo.backup_stdin_command("shop/db/shop.dump", ["drkit", "kind:db"]) == [
            "restic",
            "backup",
            "--stdin",
            "--stdin-filename",
            "shop/db/shop.dump",
            "--host",
            "prod-01",
            "--json",
            "--quiet",
            "--tag",
            "drkit",
            "--tag",
            "kind:db",
        ]

    def test_backup_bytes(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(0, SUMMARY + "\n", "")]
        summary = repo.backup_bytes('{"a": 1}', "runs/r1.json", ["drkit"])
        assert summary == restic_module.BackupSummary("abc123", 42, 7)
        call = fake.calls[0]
        assert call["args"][:5] == [
            "restic",
            "backup",
            "--stdin",
            "--stdin-filename",
            "runs/r1.json",
        ]
        assert call["input"] == '{"a": 1}'

    def test_backup_paths(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(0, SUMMARY, "")]
        repo.backup_paths(["/etc/nginx", "-weird"], ["drkit"], excludes=["*.log"])
        assert fake.argv[0][-5:] == ["--exclude", "*.log", "--", "/etc/nginx", "-weird"]
        assert fake.calls[0]["timeout"] == restic_module.BACKUP_TIMEOUT_S

    def test_backup_failure(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(1, "", "Fatal: no space left")]
        with pytest.raises(CommandError, match="restic backup failed with exit code 1"):
            repo.backup_bytes("x", "f", [])


class TestParseSummary:
    def test_picks_the_summary_among_status_lines(self) -> None:
        stdout = '{"message_type":"status","percent_done":0.5}\n' + SUMMARY + "\n\n"
        assert restic_module.parse_backup_summary(stdout).snapshot_id == "abc123"

    @pytest.mark.parametrize("stdout", ["", "not json", '{"message_type":"status"}'])
    def test_missing_summary(self, stdout: str) -> None:
        with pytest.raises(CommandError, match="did not report a summary"):
            restic_module.parse_backup_summary(stdout)


class TestQueries:
    def test_snapshots_are_filtered_and_sorted(self, repo: Restic, fake: FakeRun) -> None:
        data = [
            snapshot_json("bbbb", ["drkit", "run:r2"], "2026-10-29T04:00:00Z"),
            snapshot_json("aaaa", ["drkit", "run:r1"], "2026-10-29T03:00:00+00:00"),
        ]
        fake.responses = [(0, json.dumps(data), "")]
        snaps = repo.snapshots(["drkit", "run:r1"], ["drkit", "run:r2"], [])
        assert fake.argv[0] == [
            "restic",
            "snapshots",
            "--json",
            "--host",
            "prod-01",
            "--tag",
            "drkit,run:r1",
            "--tag",
            "drkit,run:r2",
        ]
        assert [s.short_id for s in snaps] == ["aaaa", "bbbb"]
        assert snaps[0].time == datetime(2026, 10, 29, 3, tzinfo=UTC)
        assert snaps[0].tag_value("run") == "r1"
        assert snaps[0].tag_value("db") is None

    def test_empty_output(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(0, "", "")]
        assert repo.snapshots() == []

    def test_dump(self, repo: Restic, fake: FakeRun) -> None:
        fake.responses = [(0, "content", "")]
        assert repo.dump("abc", "/runs/r1.json") == "content"
        assert fake.argv[0] == ["restic", "dump", "abc", "/runs/r1.json"]


class TestRetention:
    def test_retention_args(self) -> None:
        assert restic_module.retention_args(Retention(daily=7, monthly=6)) == [
            "--keep-daily",
            "7",
            "--keep-monthly",
            "6",
        ]

    def test_plan_groups_by_host_only(self, repo: Restic, fake: FakeRun) -> None:
        groups = [
            {
                "keep": [snapshot_json("k1", ["run:r3"])],
                "remove": [snapshot_json("r1", ["run:r1"]), snapshot_json("r2", ["run:r2"])],
            },
            {"keep": [snapshot_json("k2", ["run:r4"])], "remove": None},
        ]
        fake.responses = [(0, json.dumps(groups), "")]
        keep, remove = repo.retention_plan(Retention(weekly=4), ["drkit", "kind:meta"])
        assert fake.argv[0] == [
            "restic",
            "forget",
            "--dry-run",
            "--json",
            "--host",
            "prod-01",
            "--group-by",
            "host",
            "--tag",
            "drkit,kind:meta",
            "--keep-weekly",
            "4",
        ]
        assert [s.short_id for s in keep] == ["k1", "k2"]
        assert [s.tag_value("run") for s in remove] == ["r1", "r2"]

    def test_forget_deduplicates_and_batches(self, repo: Restic, fake: FakeRun) -> None:
        ids = [f"id{i}" for i in range(150)]
        assert repo.forget([*ids, "id0"]) == 150
        assert [len(a) for a in fake.argv] == [102, 52]
        assert fake.argv[0][:3] == ["restic", "forget", "id0"]

    def test_forget_nothing(self, repo: Restic, fake: FakeRun) -> None:
        assert repo.forget([]) == 0
        assert fake.calls == []


def test_snapshot_without_short_id() -> None:
    snap = Snapshot.from_json({"id": "0123456789abcdef", "time": "2026-10-29T03:00:00Z"})
    assert snap.short_id == "01234567"
    assert snap.tags == ()
