"""drkit prune against MinIO: orphans of runs that never finished are forgotten."""

import json
from typing import Any

import pytest

from drkit import runner
from tests.integration.conftest import HOST
from tests.integration.test_database_backup import POSTGRES

pytestmark = pytest.mark.integration


def test_prune_forgets_old_orphans_and_keeps_runs(make_drkit: Any) -> None:
    drkit = make_drkit(databases={"shop": [POSTGRES]})
    drkit("init")
    assert drkit("backup", "--only", "db").exit_code == 0
    restic = drkit.restic()

    # A run killed before writing its manifest: two days old, and one still in progress.
    for run_id, when in (("20261020T030000Z-dead", "2026-10-20 03:00:00"), ("now-0000", None)):
        args = ["restic", "backup", "--stdin", "--stdin-filename", "shop/db/shop.dump"]
        args += ["--host", HOST, "--tag", f"drkit,run:{run_id},kind:db", "--quiet"]
        if when:
            args += ["--time", when]
        runner.run(args, timeout=60, env=restic.env, input="partial dump")

    result = drkit("--json", "prune")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "orphan_runs": ["20261020T030000Z-dead"],
        "forgotten_snapshots": 1,
    }
    runs = {snap.tag_value("run") for snap in restic.snapshots()}
    assert "20261020T030000Z-dead" not in runs
    assert "now-0000" in runs
    assert len(runs) == 2
