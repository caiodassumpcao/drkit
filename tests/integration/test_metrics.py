"""Real commands keep metrics.textfile up to date, in a format promtool accepts."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import Stack
from tests.integration.test_database_backup import POSTGRES

pytestmark = pytest.mark.integration


def samples(path: Path) -> dict[str, str]:
    return dict(
        line.rsplit(" ", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_commands_refresh_the_metrics_file(make_drkit: Any, stack: Stack, tmp_path: Path) -> None:
    textfile_dir = tmp_path / "textfile_collector"
    textfile_dir.mkdir()
    textfile = textfile_dir / "drkit.prom"
    drkit = make_drkit(databases={"shop": [POSTGRES]}, metrics=textfile)

    assert drkit("init").exit_code == 0
    doctor = drkit("--json", "doctor")
    assert '"category": "metrics"' in doctor.stdout
    assert drkit("backup", "--only", "db").exit_code == 0
    after_backup = samples(textfile)
    assert after_backup['drkit_last_run_success{host="it-host"}'] == "1"
    assert after_backup['drkit_last_run_items{host="it-host",status="ok"}'] == "1"
    assert int(after_backup['drkit_last_run_bytes_added{host="it-host"}']) > 0
    assert 'drkit_project_last_success_timestamp_seconds{host="it-host",project="shop"}' in (
        after_backup
    )
    assert not any(key.startswith("drkit_last_verify") for key in after_backup)

    assert drkit("verify", "--read-data-subset", "0").exit_code == 0
    assert samples(textfile)['drkit_last_verify_success{host="it-host"}'] == "1"

    assert drkit("restore", "db", "shop", "--yes").exit_code == 0
    restored = samples(textfile)
    assert restored['drkit_last_restore_success{host="it-host",kind="db"}'] == "1"
    assert float(restored['drkit_last_restore_rto_seconds{host="it-host",kind="db"}']) > 0

    assert drkit("prune").exit_code == 0
    final = samples(textfile)
    assert final['drkit_last_prune_success{host="it-host"}'] == "1"
    assert textfile.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in textfile_dir.iterdir()] == ["drkit.prom"]

    printed = drkit("metrics").stdout
    assert printed == textfile.read_text()
    if shutil.which("promtool"):
        check = subprocess.run(
            ["promtool", "check", "metrics"],
            input=printed,
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stdout + check.stderr


def test_unwritable_metrics_never_fail_a_backup(make_drkit: Any, tmp_path: Path) -> None:
    drkit = make_drkit(databases={"shop": [POSTGRES]}, metrics=tmp_path / "missing" / "drkit.prom")
    assert drkit("init").exit_code == 0
    result = drkit("backup", "--only", "db")
    assert result.exit_code == 0, result.output
    assert "metrics.not_written" in result.output
    assert not (tmp_path / "missing").exists()
