"""drkit's metrics and example alerting rules, checked by Prometheus' own promtool.

Skipped where promtool is not installed; the CI job "Metrics" installs a pinned release and
requires it.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drkit import cli, metrics, state
from drkit.config import LoadedConfig
from tests.unit.test_metrics import full_state, manifest

PROMETHEUS = Path(__file__).resolve().parents[2] / "examples" / "prometheus"

# CI sets DRKIT_REQUIRE_PROMTOOL=1: there a missing promtool fails instead of skipping.
pytestmark = pytest.mark.skipif(
    shutil.which("promtool") is None and not os.environ.get("DRKIT_REQUIRE_PROMTOOL"),
    reason="promtool not installed",
)


def promtool(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["promtool", *args], input=stdin, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("current", [full_state(), state.State()], ids=["full", "empty"])
def test_metrics_pass_promtool(current: state.State) -> None:
    result = promtool("check", "metrics", stdin=metrics.render(metrics.collect(current, "h")))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout + result.stderr == "", "no lint warnings either"


def test_file_written_by_drkit_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
    state.save(full_state(), tmp_path / "state")
    monkeypatch.setattr(
        cli, "load_config", lambda path: LoadedConfig(path=path, manifest=manifest())
    )
    target = tmp_path / "drkit.prom"
    assert CliRunner().invoke(cli.app, ["metrics", "--output", str(target)]).exit_code == 0
    result = promtool("check", "metrics", stdin=target.read_text())
    assert result.returncode == 0, result.stdout + result.stderr


def test_alerting_rules() -> None:
    checked = promtool("check", "rules", str(PROMETHEUS / "drkit.rules.yml"))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    tested = subprocess.run(
        ["promtool", "test", "rules", "drkit.rules.test.yml"],
        cwd=PROMETHEUS,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
