import json
import os
from pathlib import Path

import httpx
import pytest

from drkit import lock, runner
from drkit.config import Notifications
from drkit.errors import ConfigError, DrkitError, LockHeldError
from drkit.restore import steps
from drkit.restore.steps import RestorePlan, Step, StepResult


def plan(*actions: object) -> tuple[RestorePlan, list[int]]:
    """A plan whose step ``n`` records ``n`` when it runs, then returns or raises ``action``."""
    calls: list[int] = []

    def make(number: int, action: object) -> Step:
        def run() -> str | None:
            calls.append(number)
            if isinstance(action, BaseException):
                raise action
            return action if isinstance(action, str) else None

        return Step(title=f"step {number}", action=run, plan=(f"does {number}",))

    return (
        RestorePlan(
            kind="project",
            target="shop",
            run_id="20261029T030000Z-a1f3",
            steps=[make(n, a) for n, a in enumerate(actions, start=1)],
            restore_id="r1",
        ),
        calls,
    )


def test_steps_run_in_order_and_are_timed() -> None:
    restore_plan, calls = plan("cloned at 9c1e2b7", None)
    seen: list[StepResult] = []

    report = steps.execute(restore_plan, on_step=seen.append)

    assert calls == [1, 2]
    assert report.ok
    assert [(s.number, s.title, s.status, s.detail) for s in report.steps] == [
        (1, "step 1", "ok", "cloned at 9c1e2b7"),
        (2, "step 2", "ok", None),
    ]
    assert seen == report.steps
    assert all(s.duration_s >= 0 for s in report.steps)
    assert report.finished_at is not None
    assert report.duration_s >= 0
    assert report.failed_step is None


def test_first_failure_stops_the_pipeline() -> None:
    error = DrkitError("pg_restore failed", hint="check the logs")
    restore_plan, calls = plan(None, error, None)

    report = steps.execute(restore_plan)

    assert calls == [1, 2]
    assert not report.ok
    assert [s.status for s in report.steps] == ["ok", "failed", "not_run"]
    assert report.failed_step is report.steps[1]
    assert report.steps[1].error == "pg_restore failed (hint: check the logs)"


def test_unexpected_errors_are_recorded() -> None:
    restore_plan, _ = plan(ValueError("boom"))
    report = steps.execute(restore_plan)
    assert report.steps[0].error == "unexpected error: ValueError: boom"


def test_resume_skips_earlier_steps() -> None:
    restore_plan, calls = plan(None, None, None)
    report = steps.execute(restore_plan, from_step=2)
    assert calls == [2, 3]
    assert [s.status for s in report.steps] == ["skipped", "ok", "ok"]
    assert report.ok


@pytest.mark.parametrize("from_step", [0, 4])
def test_from_step_out_of_range(from_step: int) -> None:
    restore_plan, calls = plan(None, None, None)
    with pytest.raises(ConfigError, match="--from-step must be between 1 and 3"):
        steps.execute(restore_plan, from_step=from_step)
    assert calls == []


def test_secrets_never_reach_the_report() -> None:
    runner.redactor.add("hunter2-secret")
    try:
        restore_plan, _ = plan("password hunter2-secret", DrkitError("bad hunter2-secret"))
        report = steps.execute(restore_plan)
    finally:
        runner.redactor.clear()
    assert report.steps[0].detail == "password ***"
    assert report.steps[1].error == "bad ***"


def test_run_saves_the_report(tmp_path: Path) -> None:
    restore_plan, _ = plan("ok", DrkitError("failed"))

    report = steps.run(restore_plan, lock_path=tmp_path / "lock", state_directory=tmp_path)

    assert report.report_path == tmp_path / "reports" / "restore-r1.json"
    data = json.loads(report.report_path.read_text())
    assert data["status"] == "failed"
    assert data["kind"] == "project"
    assert data["run_id"] == "20261029T030000Z-a1f3"
    assert [s["status"] for s in data["steps"]] == ["ok", "failed"]
    assert data["report_path"] == str(report.report_path)
    assert (tmp_path / "reports").stat().st_mode & 0o777 == 0o700


def test_run_holds_the_lock(tmp_path: Path) -> None:
    def blocked() -> str:
        with lock.acquire(tmp_path / "lock"):
            return "lock was free"

    restore_plan = RestorePlan("db", "shop/db/shop", "r", [Step("try", blocked)])
    report = steps.run(restore_plan, lock_path=tmp_path / "lock", state_directory=tmp_path)
    assert report.steps[0].error is not None
    assert report.steps[0].error.startswith("another drkit process")

    with lock.acquire(tmp_path / "lock"), pytest.raises(LockHeldError):
        steps.run(restore_plan, lock_path=tmp_path / "lock", state_directory=tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_unwritable_report_directory_is_reported(tmp_path: Path) -> None:
    readonly = tmp_path / "state"
    readonly.mkdir()
    readonly.chmod(0o500)
    restore_plan, _ = plan(None)
    try:
        report = steps.run(restore_plan, lock_path=tmp_path / "lock", state_directory=readonly)
    finally:
        readonly.chmod(0o700)
    assert report.ok
    assert report.report_path is None
    assert report.errors[0].startswith(f"report: cannot write restore report in {readonly}")


def test_report_dict() -> None:
    restore_plan, _ = plan("done")
    data = steps.execute(restore_plan).to_dict()
    assert data["restore_id"] == "r1"
    assert data["status"] == "ok"
    assert data["report_path"] is None
    assert data["steps"][0]["detail"] == "done"


class TestNotifications:
    @pytest.fixture
    def channel(self) -> tuple[Notifications, list[str], httpx.Client]:
        received: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(json.loads(request.content)["text"])
            return httpx.Response(200)

        config = Notifications.model_validate(
            {"on": ["failure"], "webhook": {"url": "https://hooks.example.com/x"}}
        )
        return config, received, httpx.Client(transport=httpx.MockTransport(handler))

    def test_both_outcomes_are_announced(
        self, tmp_path: Path, channel: tuple[Notifications, list[str], httpx.Client]
    ) -> None:
        config, received, client = channel
        ok_plan, _ = plan("done")
        failing, _ = plan(None, DrkitError("pg_restore failed"))
        for restore_plan in (ok_plan, failing):
            report = steps.run(
                restore_plan,
                lock_path=tmp_path / "lock",
                state_directory=tmp_path,
                notifications=config,
                host="prod-01",
                http_client=client,
            )
            assert report.notify_errors == []
        assert received[0].splitlines()[0] == "drkit restore project shop on prod-01: OK"
        assert received[1].splitlines() == [
            "drkit restore project shop on prod-01: FAILED",
            "run 20261029T030000Z-a1f3 · 2 step(s) in 0s · restore r1",
            "✗ step 2 (step 2): pg_restore failed",
        ]
