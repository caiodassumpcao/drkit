import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from drkit import report, state
from drkit.config import Repository
from drkit.restic import Restic, Snapshot
from drkit.runmanifest import RunManifest
from drkit.sources.base import ItemResult

T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)


class FakeRepo(Restic):
    """Meta snapshots and the manifests they hold."""

    def __init__(self, runs: list[RunManifest]) -> None:
        repository = Repository(url="s3:https://s3.example.com/b", password_file=Path("/pw"))
        super().__init__(repository, host="prod-01", env={})
        self.runs = {f"meta-{run.run_id}": run for run in runs}
        self.dumped: list[str] = []

    def snapshots(self, *tag_groups: Sequence[str]) -> list[Snapshot]:
        return [
            Snapshot(
                sid, sid[:8], run.finished_at, "prod-01",
                ("drkit", f"run:{run.run_id}", "kind:meta", f"status:{run.status}"), (),
            )
            for sid, run in self.runs.items()
        ]  # fmt: skip

    def dump(self, snapshot_id: str, path: str) -> str:
        self.dumped.append(path)
        return self.runs[snapshot_id].to_json()


def run(run_id: str, status: str, finished: datetime, sizes: Sequence[int]) -> RunManifest:
    items = [
        ItemResult(kind="db", project="p", name=f"d{i}", bytes=size, status="ok")
        for i, size in enumerate(sizes)
    ]
    if status != "ok":
        items.append(ItemResult(kind="db", project="p", name="x", status="failed", error="e"))
    return RunManifest.model_validate(
        {
            "run_id": run_id,
            "host": "prod-01",
            "drkit_version": "0.2.0",
            "started_at": finished - timedelta(seconds=95),
            "finished_at": finished,
            "status": status,
            "items": items,
        }
    )


RUNS = [
    run("r1", "ok", T0 - timedelta(hours=3), [1024]),
    run("r2", "ok", T0 - timedelta(hours=2), [2048, 2048]),
    run("r3", "partial", T0 - timedelta(hours=1), [3 * 1024**2]),
]


def test_report_from_repository_and_state(tmp_path: Path) -> None:
    state.record_verify("r2", "ok", tmp_path)
    repo = FakeRepo(RUNS)

    status = report.build_report(repo, limit=2, state_directory=tmp_path, now=T0)

    assert [line.run_id for line in status.runs] == ["r3", "r2"]
    assert repo.dumped == ["/runs/r3.json", "/runs/r2.json"]  # only the runs shown
    r3 = status.runs[0]
    assert (r3.status, r3.items, r3.failed, r3.bytes, r3.duration_s) == (
        "partial", 2, 1, 3 * 1024**2, 95.0
    )  # fmt: skip
    assert status.last_success_run == "r2"
    assert status.rpo_s == 2 * 3600
    assert status.last_verify is not None

    data = json.loads(json.dumps(status.to_dict(), default=str))
    assert data["rpo_s"] == 7200
    assert data["last_verify"]["run_id"] == "r2"
    assert data["last_drill"] is None

    markdown = report.to_markdown(status)
    assert markdown.startswith("# drkit report: prod-01\n")
    assert "| Current RPO | 2h 00m (last successful run `r2`) |" in markdown
    assert "| Last verify | ok · run `r2` · " in markdown
    assert "| `r3` | 2026-10-29 02:00 UTC | partial | 2 | 1 | 3.0 MiB | 1m |" in markdown


def test_no_runs_and_no_verify(tmp_path: Path) -> None:
    status = report.build_report(FakeRepo([]), state_directory=tmp_path, now=T0)
    assert status.rpo_s is None
    markdown = report.to_markdown(status)
    assert "| Current RPO | unknown: no successful run |" in markdown
    assert "| Last verify | never |" in markdown
    assert markdown.endswith("No runs yet.\n")


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KiB"), (5 * 1024**3, "5.0 GiB"),
     (3 * 1024**4, "3.0 TiB")],
)  # fmt: skip
def test_human_bytes(size: int, text: str) -> None:
    assert report.human_bytes(size) == text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(42, "42s"), (60, "1m"), (3599, "59m"), (3600, "1h 00m"), (47 * 3600 + 120, "47h 02m"),
     (50 * 3600, "2d 2h")],
)  # fmt: skip
def test_human_duration(seconds: float, text: str) -> None:
    assert report.human_duration(seconds) == text
