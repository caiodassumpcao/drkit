"""drkit verify against real dumps: each database is restored into a throwaway container."""

import json
import subprocess
from typing import Any

import pytest

from tests.integration.conftest import Webhook, image_version, version_line
from tests.integration.test_database_backup import MARIADB, MYSQL, POSTGRES

pytestmark = pytest.mark.integration


def verify_containers() -> list[str]:
    return subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", "label=drkit.verify"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


def test_verify_restores_every_database(make_drkit: Any) -> None:
    postgres = {**POSTGRES, "verify_query": "SELECT item FROM orders WHERE id = 1"}
    drkit = make_drkit(databases={"shop": [postgres, MYSQL], "blog": [MARIADB]})
    drkit("init")
    assert drkit("backup", "--only", "db").exit_code == 0

    result = drkit("--json", "verify", "--read-data-subset", "100%")

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["repository_error"] is None
    databases = {db["label"]: db for db in report["databases"]}
    assert {label: (db["status"], db["image"]) for label, db in databases.items()} == {
        # The same version line as the server that was backed up.
        "shop/db/shop": ("ok", f"postgres:{version_line(image_version('db'), 1)}"),
        "shop/db/legacy": ("ok", f"mysql:{version_line(image_version('mysql'), 2)}"),
        "blog/db/blog": ("ok", f"mariadb:{version_line(image_version('mariadb'), 2)}"),
    }
    assert all(db["tables"] >= 1 for db in databases.values())
    assert verify_containers() == []
    state = json.loads((drkit.state_dir / "state.json").read_text())
    assert state["last_verify"]["status"] == "ok"
    assert state["last_verify"]["run_id"] == report["run_id"]


def test_failed_verification_is_reported(make_drkit: Any, webhook: Webhook) -> None:
    postgres = {**POSTGRES, "verify_query": "SELECT * FROM no_such_table"}
    drkit = make_drkit(databases={"shop": [postgres]}, webhook_url=webhook.url)
    drkit("init")
    assert drkit("backup", "--only", "db").exit_code == 0

    result = drkit("verify", "--read-data-subset", "0")

    assert result.exit_code == 1, result.output
    [message] = webhook.messages
    assert message.startswith("drkit verify on it-host: FAILED")
    assert '✗ shop/db/shop: psql failed with exit code 1: ERROR:  relation "no_such_table"' in (
        message
    )
    assert verify_containers() == []
