"""End-to-end database backups against real MinIO, PostgreSQL, MySQL and MariaDB."""

import json
import secrets
from typing import Any

import pytest

from drkit import docker, runner
from drkit import restic as restic_module
from drkit.runmanifest import RunManifest
from tests.integration.conftest import HOST, Stack, Webhook

pytestmark = pytest.mark.integration

POSTGRES = {"engine": "postgres", "service": "db", "database": "shop"}
MYSQL = {"engine": "mysql", "service": "mysql", "database": "legacy"}
# No user_env / password_env: the MARIADB_* variables of the official image are found by
# the default fallback.
MARIADB = {"engine": "mysql", "service": "mariadb", "database": "blog"}
ALL_ENGINES = {"shop": [POSTGRES, MYSQL], "blog": [MARIADB]}


def snapshots_by_path(drkit: Any) -> dict[str, Any]:
    return {snap.paths[0]: snap for snap in drkit.restic().snapshots()}


def test_backs_up_both_engines_with_tags_and_run_manifest(make_drkit: Any, stack: Stack) -> None:
    drkit = make_drkit(databases=ALL_ENGINES)

    assert drkit("init").exit_code == 0
    result = drkit("backup", "--only", "db")
    assert result.exit_code == 0, result.output

    run = RunManifest.from_json(drkit("--json", "runs", "show", "latest").stdout)
    assert run.status == "ok"
    assert run.host == HOST
    items = {f"{i.project}/{i.name}": i for i in run.items}
    assert set(items) == {"shop/shop", "shop/legacy", "blog/blog"}
    assert all(i.status == "ok" and i.bytes > 0 for i in run.items)
    assert items["shop/shop"].engine_version is not None
    assert items["shop/shop"].engine_version.startswith("16.")
    assert items["shop/legacy"].engine_version == "8.4.9"
    assert items["blog/blog"].engine_version is not None
    assert "MariaDB" in items["blog/blog"].engine_version

    snaps = snapshots_by_path(drkit)
    base = {"drkit", f"host:{HOST}", f"run:{run.run_id}"}
    expected = {
        "/shop/db/shop.dump": {*base, "kind:db", "project:shop", "db:shop"},
        "/shop/db/shop.globals.sql": {*base, "kind:db", "project:shop", "db:shop"},
        "/shop/db/legacy.sql": {*base, "kind:db", "project:shop", "db:legacy"},
        "/blog/db/blog.sql": {*base, "kind:db", "project:blog", "db:blog"},
        f"/runs/{run.run_id}.json": {*base, "kind:meta", "status:ok"},
    }
    assert {path: set(snap.tags) for path, snap in snaps.items()} == expected
    assert all(snap.hostname == HOST for snap in snaps.values())
    # The manifest points at the exact snapshots of this run.
    assert items["shop/shop"].snapshot == snaps["/shop/db/shop.dump"].id
    assert items["shop/shop"].globals_snapshot == snaps["/shop/db/shop.globals.sql"].id
    assert items["shop/legacy"].snapshot == snaps["/shop/db/legacy.sql"].id

    # The dumps hold the seeded data.
    restic = drkit.restic()
    assert "drkit-seed-mysql" in restic.dump(snaps["/shop/db/legacy.sql"].id, "/shop/db/legacy.sql")
    assert "drkit-seed-mariadb" in restic.dump(snaps["/blog/db/blog.sql"].id, "/blog/db/blog.sql")
    assert "CREATE ROLE app" in restic.dump(
        snaps["/shop/db/shop.globals.sql"].id, "/shop/db/shop.globals.sql"
    )
    container = docker.compose_container(stack.project, "db")
    restored = runner.pipe(
        ["restic", "dump", snaps["/shop/db/shop.dump"].id, "/shop/db/shop.dump"],
        docker.exec_args(container, ["pg_restore", "-f", "-"], stdin=True),
        timeout=60,
        producer_env=restic.env,
    )
    assert restored.ok
    assert "drkit-seed-postgres" in restored.consumer.stdout

    runs = json.loads(drkit("--json", "runs", "list").stdout)
    assert [(r["run_id"], r["status"]) for r in runs] == [(run.run_id, "ok")]
    state = json.loads((drkit.state_dir / "state.json").read_text())
    assert state["last_success"]["run_id"] == run.run_id


def test_failed_dump_discards_snapshot_and_reports_partial(
    make_drkit: Any, webhook: Webhook
) -> None:
    missing = {"engine": "postgres", "service": "db", "database": "does_not_exist"}
    drkit = make_drkit(databases={"shop": [POSTGRES, missing]}, webhook_url=webhook.url)
    drkit("init")

    result = drkit("backup", "--only", "db")

    assert result.exit_code == 1, result.output
    run = RunManifest.from_json(drkit("--json", "runs", "show").stdout)
    assert run.status == "partial"
    ok, failed = run.items
    assert ok.status == "ok"
    assert failed.status == "failed"
    assert failed.snapshot is None
    assert failed.error is not None
    assert failed.error.startswith("pg_dump failed with exit code 1")
    assert 'database "does_not_exist" does not exist' in failed.error

    # restic did store the (empty) stream; drkit forgot it right away.
    tags = [set(snap.tags) for snap in drkit.restic().snapshots()]
    assert not [t for t in tags if "db:does_not_exist" in t]
    assert [t for t in tags if "kind:meta" in t] == [
        {"drkit", f"host:{HOST}", f"run:{run.run_id}", "kind:meta", "status:partial"}
    ]

    [message] = webhook.messages
    assert message.startswith(f"drkit backup on {HOST}: PARTIAL")
    assert "✗ shop/db/does_not_exist: pg_dump failed with exit code 1" in message


def test_retention_drops_whole_runs(make_drkit: Any) -> None:
    drkit = make_drkit(databases={"shop": [POSTGRES, MYSQL]}, retention={"daily": 1})
    drkit("init")

    assert drkit("backup", "--only", "db").exit_code == 0
    first = RunManifest.from_json(drkit("--json", "runs", "show").stdout).run_id
    result = drkit("--json", "backup", "--only", "db")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    second = payload["run"]["run_id"]
    assert payload["retention"] == {"removed_runs": [first], "forgotten_snapshots": 4}
    remaining = drkit.restic().snapshots()
    assert {snap.tag_value("run") for snap in remaining} == {second}
    assert len(remaining) == 4  # postgres dump + globals, mysql dump, run manifest


def test_doctor_checks_repository_access(make_drkit: Any) -> None:
    drkit = make_drkit(databases={"shop": [POSTGRES]})

    result = drkit("--json", "doctor")
    checks = {c["category"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["repository"]["status"] == "fail"
    assert checks["repository"]["detail"] == "not initialized"
    assert result.exit_code == 1

    drkit("init")
    result = drkit("--json", "doctor")
    checks = {c["category"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["repository"]["status"] == "ok"
    assert result.exit_code == 0, result.stdout


def test_init_is_idempotent(make_drkit: Any) -> None:
    drkit = make_drkit(databases={})
    assert json.loads(drkit("--json", "init").stdout)["created"] is True
    assert json.loads(drkit("--json", "init").stdout)["created"] is False


def test_missing_bucket_fails_fast_and_init_creates_it(
    make_drkit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restic_module, "PROBE_TIMEOUT_S", 5.0)
    drkit = make_drkit(databases={}, bucket=f"fresh-{secrets.token_hex(4)}")

    result = drkit("--json", "doctor")
    checks = {c["category"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["repository"]["status"] == "fail"
    assert checks["repository"]["detail"].endswith("did not answer within 5s")
    assert result.exit_code == 1

    assert json.loads(drkit("--json", "init").stdout)["created"] is True
    assert drkit("doctor").exit_code == 0
