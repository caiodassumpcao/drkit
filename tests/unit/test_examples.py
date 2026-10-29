from pathlib import Path

from drkit import config
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
