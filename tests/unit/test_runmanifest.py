import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from drkit import runmanifest
from drkit.errors import DrkitError
from drkit.restic import Snapshot
from drkit.runmanifest import RunManifest, RunSummary
from drkit.sources.base import ItemResult

STARTED = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)

ITEM = ItemResult(
    kind="db",
    project="artesempreco",
    name="artes",
    engine="postgres",
    engine_version="16.4",
    snapshot="3f2a9c1d",
    bytes=48213,
    duration_s=3.2,
    status="ok",
)


def manifest(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "run_id": "20261027T030000Z-a1f3",
        "host": "prod-01",
        "drkit_version": "0.1.0",
        "started_at": STARTED,
        "finished_at": STARTED + timedelta(minutes=2, seconds=41),
        "status": "ok",
        "items": [ITEM],
        **overrides,
    }
    return RunManifest(**fields)


def test_serializes_to_the_design_schema() -> None:
    data = json.loads(manifest().to_json())
    assert data == {
        "schema": 1,
        "run_id": "20261027T030000Z-a1f3",
        "host": "prod-01",
        "drkit_version": "0.1.0",
        "started_at": "2026-10-27T03:00:00Z",
        "finished_at": "2026-10-27T03:02:41Z",
        "status": "ok",
        "items": [
            {
                "kind": "db",
                "project": "artesempreco",
                "name": "artes",
                "engine": "postgres",
                "engine_version": "16.4",
                "snapshot": "3f2a9c1d",
                "globals_snapshot": None,
                "bytes": 48213,
                "duration_s": 3.2,
                "status": "ok",
                "error": None,
            }
        ],
        "projects": {},
    }


def test_round_trip() -> None:
    original = manifest(
        status="partial", items=[ITEM, ITEM.model_copy(update={"status": "failed"})]
    )
    restored = RunManifest.from_json(original.to_json())
    assert restored == original
    assert [i.status for i in restored.failed_items] == ["failed"]


def test_rejects_unknown_schema_and_fields() -> None:
    data = json.loads(manifest().to_json())
    with pytest.raises(ValidationError):
        RunManifest.from_json(json.dumps({**data, "schema": 2}))
    with pytest.raises(ValidationError):
        RunManifest.from_json(json.dumps({**data, "surprise": True}))


def test_new_run_id_format_and_uniqueness() -> None:
    now = datetime(2026, 10, 27, 0, 0, 5, tzinfo=timezone(timedelta(hours=-3)))
    run_id = runmanifest.new_run_id(now)
    assert run_id.startswith("20261027T030005Z-")
    assert runmanifest.RUN_ID_RE.match(run_id)
    assert len({runmanifest.new_run_id(now) for _ in range(50)}) > 1
    assert runmanifest.RUN_ID_RE.match(runmanifest.new_run_id())


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "ok"),
        (["ok", "ok"], "ok"),
        (["ok", "failed"], "partial"),
        (["failed", "failed"], "failed"),
    ],
)
def test_run_status(statuses: list[str], expected: str) -> None:
    items = [ITEM.model_copy(update={"status": s}) for s in statuses]
    assert runmanifest.run_status(items) == expected


def test_tags_and_filename() -> None:
    assert runmanifest.manifest_filename("r1") == "runs/r1.json"
    assert runmanifest.manifest_tags("prod-01", "r1", "partial") == [
        "drkit",
        "host:prod-01",
        "run:r1",
        "kind:meta",
        "status:partial",
    ]


def meta_snapshot(run_id: str, status: str, hour: int) -> Snapshot:
    return Snapshot(
        id=f"id-{run_id}",
        short_id=run_id,
        time=datetime(2026, 10, 27, hour, tzinfo=UTC),
        hostname="prod-01",
        tags=("drkit", f"run:{run_id}", "kind:meta", f"status:{status}"),
        paths=(f"/runs/{run_id}.json",),
    )


class FakeRestic:
    def __init__(self, snapshots: list[Snapshot], documents: dict[str, str] | None = None) -> None:
        self._snapshots = snapshots
        self.documents = documents or {}
        self.queries: list[tuple[list[str], ...]] = []
        self.dumps: list[tuple[str, str]] = []

    def snapshots(self, *groups: list[str]) -> list[Snapshot]:
        self.queries.append(groups)
        wanted = set(groups[0]) - {"drkit"}
        return [s for s in self._snapshots if wanted <= set(s.tags)]

    def dump(self, snapshot_id: str, path: str) -> str:
        self.dumps.append((snapshot_id, path))
        return self.documents[path]


def test_list_runs_reads_only_tags() -> None:
    restic = FakeRestic([meta_snapshot("r1", "ok", 3), meta_snapshot("r2", "partial", 4)])
    runs = runmanifest.list_runs(restic)  # type: ignore[arg-type]
    assert runs == [
        RunSummary("r1", "ok", datetime(2026, 10, 27, 3, tzinfo=UTC), "id-r1"),
        RunSummary("r2", "partial", datetime(2026, 10, 27, 4, tzinfo=UTC), "id-r2"),
    ]
    assert restic.queries == [(["drkit", "kind:meta"],)]
    assert restic.dumps == []


def test_summary_of_untagged_snapshot() -> None:
    snap = Snapshot("x", "x", STARTED, "h", ("drkit",), ())
    assert RunSummary.from_snapshot(snap).run_id == "?"


class TestLoadRun:
    RUN = "20261027T030000Z-a1f3"

    def test_by_id(self) -> None:
        doc = manifest().to_json()
        restic = FakeRestic([meta_snapshot(self.RUN, "ok", 3)], {f"/runs/{self.RUN}.json": doc})
        assert runmanifest.load_run(restic, self.RUN) == manifest()  # type: ignore[arg-type]
        assert restic.queries == [(["drkit", "kind:meta", f"run:{self.RUN}"],)]
        assert restic.dumps == [(f"id-{self.RUN}", f"/runs/{self.RUN}.json")]

    def test_latest(self) -> None:
        latest = "20261027T040000Z-bbbb"
        restic = FakeRestic(
            [meta_snapshot(self.RUN, "ok", 3), meta_snapshot(latest, "ok", 4)],
            {f"/runs/{latest}.json": manifest(run_id=latest).to_json()},
        )
        assert runmanifest.load_run(restic).run_id == latest  # type: ignore[arg-type]

    def test_not_found(self) -> None:
        with pytest.raises(DrkitError, match=f"run {self.RUN} not found"):
            runmanifest.load_run(FakeRestic([]), self.RUN)  # type: ignore[arg-type]
        with pytest.raises(DrkitError, match="no backup run found"):
            runmanifest.load_run(FakeRestic([]))  # type: ignore[arg-type]

    def test_invalid_id(self) -> None:
        with pytest.raises(DrkitError, match="invalid run ID 'x,y'"):
            runmanifest.load_run(FakeRestic([]), "x,y")  # type: ignore[arg-type]
