import io
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from drkit import logging as drkit_logging
from drkit import orchestrator, state
from drkit.config import LoadedConfig, Manifest, Repository, Retention
from drkit.errors import ConfigError, ExitCode, LockHeldError
from drkit.lock import acquire
from drkit.orchestrator import BackupReport
from drkit.restic import BackupSummary, Restic, Snapshot
from drkit.runmanifest import RunManifest
from drkit.sources.base import BackupContext, ItemResult
from drkit.sources.mysql import MysqlSource
from drkit.sources.postgres import PostgresSource

T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)

MANIFEST = Manifest.model_validate(
    {
        "version": 1,
        "host": "prod-01",
        "repository": {"url": "s3:https://s3.example.com/b", "password_file": "/pw"},
        "retention": {"daily": 7},
        "projects": [
            {
                "name": "shop",
                "path": "/srv/shop",
                "git": {"remote": "git@example.com:shop.git"},
                "compose": {"files": ["compose.yaml"]},
                "databases": [
                    {"engine": "postgres", "service": "db", "database": "shop"},
                    {"engine": "mysql", "service": "mysql", "database": "legacy"},
                ],
            },
            {
                "name": "blog",
                "path": "/srv/blog",
                "git": {"remote": "git@example.com:blog.git"},
                "compose": {"files": ["compose.yaml"]},
                "databases": [{"engine": "postgres", "service": "db", "database": "blog"}],
            },
        ],
        "notifications": {"on": ["failure", "success"], "webhook": {"url": "https://hooks.test/x"}},
    }
)


class MemoryRestic(Restic):
    """In-memory repository: records snapshots, honours tag filters and forget."""

    def __init__(self, *, ready: bool = True) -> None:
        super().__init__(
            Repository(url="s3:https://s3.example.com/b", password_file=Path("/pw")),
            host="prod-01",
            env={},
        )
        self.ready = ready
        self.store: dict[str, tuple[Snapshot, str]] = {}
        self.plan_remove: list[str] = []  # run IDs the retention policy drops
        self.fail_on: set[str] = set()

    def add(self, tags: Sequence[str], content: str = "", when: datetime = T0) -> str:
        sid = f"snap{len(self.store):03d}"
        self.store[sid] = (Snapshot(sid, sid, when, self.host, tuple(tags), ()), content)
        return sid

    def ensure_ready(self) -> None:
        if not self.ready:
            raise ConfigError("restic repository is not initialized", hint="run `drkit init`")

    def backup_bytes(self, data: str, filename: str, tags: Sequence[str]) -> BackupSummary:
        if "backup_bytes" in self.fail_on:
            raise ConfigError("disk full")
        return BackupSummary(self.add(tags, data), len(data), len(data))

    def snapshots(self, *tag_groups: Sequence[str]) -> list[Snapshot]:
        groups = [set(g) for g in tag_groups if g]
        return [
            snap
            for snap, _ in self.store.values()
            if not groups or any(g <= set(snap.tags) for g in groups)
        ]

    def retention_plan(
        self, retention: Retention, tags: Sequence[str]
    ) -> tuple[list[Snapshot], list[Snapshot]]:
        if "retention" in self.fail_on:
            raise ConfigError("restic forget failed")
        metas = self.snapshots(tags)
        remove = [s for s in metas if s.tag_value("run") in self.plan_remove]
        return [s for s in metas if s not in remove], remove

    def forget(self, snapshot_ids: Iterable[str]) -> int:
        ids = list(snapshot_ids)
        for sid in ids:
            del self.store[sid]
        return len(ids)

    def runs(self) -> list[RunManifest]:
        return [
            RunManifest.from_json(content)
            for snap, content in self.store.values()
            if "kind:meta" in snap.tags
        ]


class FakeSource:
    kind = "db"

    def __init__(self, project: str, name: str, *, fail: str | None = None, crash: bool = False):
        self.project: str | None = project
        self.name = name
        self.fail = fail
        self.crash = crash

    def tags(self, ctx: BackupContext) -> list[str]:
        return ctx.tags(self.kind, project=self.project, db=self.name)

    def backup(self, ctx: BackupContext) -> ItemResult:
        if self.crash:
            raise RuntimeError("boom")
        if self.fail:
            return ItemResult(
                kind="db", project=self.project, name=self.name, status="failed", error=self.fail
            )
        assert isinstance(ctx.restic, MemoryRestic)
        sid = ctx.restic.add(self.tags(ctx))
        return ItemResult(
            kind="db", project=self.project, name=self.name, status="ok", snapshot=sid, bytes=10
        )


class Webhook:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.messages.append(json.loads(request.content)["text"])
        return httpx.Response(200)


@pytest.fixture
def webhook() -> Webhook:
    return Webhook()


def run(
    tmp_path: Path,
    restic: MemoryRestic,
    webhook: Webhook,
    sources: list[FakeSource] | None,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> BackupReport:
    if sources is not None:
        monkeypatch.setattr(orchestrator, "build_sources", lambda *_a, **_k: sources)
    return orchestrator.run_backup(
        LoadedConfig(path=tmp_path / "dr.yaml", manifest=MANIFEST),
        restic=restic,
        lock_path=tmp_path / "drkit.lock",
        state_directory=tmp_path / "state",
        http_client=httpx.Client(transport=httpx.MockTransport(webhook)),
        **kwargs,
    )


class TestRunBackup:
    def test_successful_run(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restic = MemoryRestic()
        sources = [FakeSource("shop", "shop"), FakeSource("shop", "legacy")]

        report = run(tmp_path, restic, webhook, sources, monkeypatch)

        assert report.ok
        assert report.exit_code is ExitCode.OK
        run_manifest = report.manifest
        assert run_manifest.status == "ok"
        assert [i.name for i in run_manifest.items] == ["shop", "legacy"]
        assert run_manifest.host == "prod-01"

        # Item snapshots and the meta snapshot share the run tag.
        run_tag = f"run:{run_manifest.run_id}"
        assert all(run_tag in snap.tags for snap, _ in restic.store.values())
        meta = [snap for snap, _ in restic.store.values() if "kind:meta" in snap.tags]
        assert len(meta) == 1
        assert "status:ok" in meta[0].tags
        assert restic.runs() == [run_manifest]

        saved = state.load(tmp_path / "state")
        assert saved.last_success is not None
        assert saved.last_success.run_id == run_manifest.run_id
        assert webhook.messages[0].startswith("drkit backup on prod-01: OK\n")

    def test_failed_item_makes_a_partial_run(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restic = MemoryRestic()
        sources = [
            FakeSource("shop", "shop"),
            FakeSource("shop", "missing", fail="pg_dump failed with exit code 1"),
        ]

        report = run(tmp_path, restic, webhook, sources, monkeypatch)

        assert report.manifest.status == "partial"
        assert report.exit_code is ExitCode.PARTIAL_FAILURE
        assert restic.runs()[0].status == "partial"
        assert [s.tags[-1] for s, _ in restic.store.values() if "kind:meta" in s.tags] == [
            "status:partial"
        ]
        [message] = webhook.messages
        assert message.splitlines()[0] == "drkit backup on prod-01: PARTIAL"
        assert "1 ok, 1 failed" in message
        assert "✗ shop/db/missing: pg_dump failed with exit code 1" in message
        last = state.load(tmp_path / "state")
        assert last.last_run is not None
        assert last.last_run.status == "partial"
        assert last.last_success is None

    def test_crashing_source_does_not_abort_the_run(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources = [FakeSource("shop", "a", crash=True), FakeSource("shop", "b")]
        report = run(tmp_path, MemoryRestic(), webhook, sources, monkeypatch)
        crashed, ok = report.manifest.items
        assert crashed.status == "failed"
        assert crashed.error == "unexpected error: RuntimeError: boom"
        assert ok.status == "ok"

    def test_all_items_failed(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = run(
            tmp_path, MemoryRestic(), webhook, [FakeSource("s", "a", fail="x")], monkeypatch
        )
        assert report.manifest.status == "failed"
        assert report.exit_code is ExitCode.PARTIAL_FAILURE

    def test_uninitialized_repository_aborts_and_notifies(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ConfigError, match="not initialized"):
            run(tmp_path, MemoryRestic(ready=False), webhook, [], monkeypatch)
        assert webhook.messages == [
            "drkit backup on prod-01: FAILED\ncould not start: restic repository is not initialized"
        ]

    def test_failure_to_store_the_run_manifest(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restic = MemoryRestic()
        restic.fail_on = {"backup_bytes"}
        with pytest.raises(ConfigError, match="disk full"):
            run(tmp_path, restic, webhook, [FakeSource("s", "a")], monkeypatch)
        assert "cannot store run manifest: disk full" in webhook.messages[0]

    def test_retention_failure_is_reported_but_keeps_the_run(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restic = MemoryRestic()
        restic.fail_on = {"retention"}
        report = run(tmp_path, restic, webhook, [FakeSource("s", "a")], monkeypatch)
        assert report.manifest.status == "ok"
        assert report.errors == ["retention: restic forget failed"]
        assert report.exit_code is ExitCode.PARTIAL_FAILURE
        assert "✗ retention: restic forget failed" in webhook.messages[0]

    def test_state_failure_is_reported(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "state").write_text("not a directory")
        report = run(tmp_path, MemoryRestic(), webhook, [FakeSource("s", "a")], monkeypatch)
        assert report.errors[0].startswith("state: cannot ")

    def test_lock_is_held_during_the_run(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with acquire(tmp_path / "drkit.lock"), pytest.raises(LockHeldError):
            run(tmp_path, MemoryRestic(), webhook, [], monkeypatch)

    def test_logs_carry_the_run_id(
        self, tmp_path: Path, webhook: Webhook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = io.StringIO()
        drkit_logging.configure(json=True, stream=stream)
        try:
            report = run(tmp_path, MemoryRestic(), webhook, [FakeSource("s", "a")], monkeypatch)
        finally:
            structlog.reset_defaults()
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        backup_events = [e for e in events if e["event"].startswith("backup.")]
        assert [e["event"] for e in backup_events] == [
            "backup.start",
            "backup.item",
            "backup.finish",
        ]
        assert {e["run_id"] for e in backup_events} == {report.manifest.run_id}
        assert structlog.contextvars.get_contextvars() == {}


class TestRetention:
    def test_drops_whole_runs(self) -> None:
        restic = MemoryRestic()
        for run_id in ("r1", "r2", "r3"):
            restic.add(["drkit", f"run:{run_id}", "kind:db", "db:shop"])
            restic.add(["drkit", f"run:{run_id}", "kind:db", "db:legacy"])
            restic.add(["drkit", f"run:{run_id}", "kind:meta"])
        restic.plan_remove = ["r1", "r2"]

        result = orchestrator.apply_retention(restic, Retention(daily=1), protect="r3")

        assert result.removed_runs == ["r1", "r2"]
        assert result.forgotten_snapshots == 6
        assert {s.tag_value("run") for s, _ in restic.store.values()} == {"r3"}

    def test_never_drops_the_current_run(self) -> None:
        restic = MemoryRestic()
        restic.add(["drkit", "run:r1", "kind:meta"])
        restic.plan_remove = ["r1"]
        result = orchestrator.apply_retention(restic, Retention(daily=1), protect="r1")
        assert result.removed_runs == []
        assert len(restic.store) == 1

    def test_nothing_to_remove(self) -> None:
        restic = MemoryRestic()
        restic.add(["drkit", "run:r1", "kind:meta"])
        assert (
            orchestrator.apply_retention(
                restic, Retention(daily=1), protect="x"
            ).forgotten_snapshots
            == 0
        )


class TestBuildSources:
    def test_all_databases_in_manifest_order(self) -> None:
        sources = orchestrator.build_sources(MANIFEST)
        assert [(type(s), s.project, s.name) for s in sources] == [
            (PostgresSource, "shop", "shop"),
            (MysqlSource, "shop", "legacy"),
            (PostgresSource, "blog", "blog"),
        ]

    def test_filter_by_project_and_kind(self) -> None:
        assert [
            s.name for s in orchestrator.build_sources(MANIFEST, project="blog", kinds=["db"])
        ] == ["blog"]

    def test_unknown_project(self) -> None:
        with pytest.raises(ConfigError, match="unknown project 'nope'"):
            orchestrator.build_sources(MANIFEST, project="nope")

    def test_unsupported_kind(self) -> None:
        with pytest.raises(ConfigError, match=r"unsupported item kind.*volume") as excinfo:
            orchestrator.build_sources(MANIFEST, kinds=["db", "volume"])
        assert excinfo.value.hint == "available in this version: db"


def test_format_message_success() -> None:
    run_manifest = RunManifest(
        run_id="r1",
        host="h",
        drkit_version="0",
        started_at=T0,
        finished_at=T0 + timedelta(seconds=41),
        status="ok",
        items=[ItemResult(kind="db", project="p", name="a", status="ok")],
    )
    assert orchestrator.format_message("h", BackupReport(run_manifest)) == (
        "drkit backup on h: OK\nrun r1 · 1 ok, 0 failed · 41s"
    )
