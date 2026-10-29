from pathlib import Path

from drkit import cli, config
from drkit.config import Manifest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def test_example_manifest_is_valid() -> None:
    path = EXAMPLES / "dr.yaml"
    raw = config.parse_yaml(path.read_text(), source=path)
    data = config.interpolate(
        raw, {"DRKIT_TG_TOKEN": "123:token", "DRKIT_WEBHOOK_URL": "https://hooks.example.com/x"}
    )

    manifest = Manifest.model_validate(data)

    assert manifest.host == "prod-01"
    assert [p.name for p in manifest.projects] == ["artesempreco"]
    assert manifest.notifications is not None
    assert manifest.notifications.on == ["failure"]
    assert manifest.system.tls is not None
    assert manifest.system.tls.strategy == "reissue"


def _unit(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_systemd_units_run_existing_commands() -> None:
    commands = {command.name for command in cli.app.registered_commands}
    services = sorted((EXAMPLES / "systemd").glob("*.service"))
    assert [s.stem for s in services] == ["drkit-backup", "drkit-prune", "drkit-verify"]
    for service in services:
        binary, subcommand = _unit(service)["ExecStart"].split()
        assert binary == "/usr/local/bin/drkit"
        assert subcommand in commands, service.name
        assert subcommand == service.stem.removeprefix("drkit-")
        timer = service.with_suffix(".timer")
        assert _unit(timer)["Persistent"] == "true", timer.name
