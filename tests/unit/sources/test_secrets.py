from collections.abc import Sequence
from pathlib import Path
from typing import cast

from drkit.config import Project
from drkit.restic import BackupSummary, Restic
from drkit.sources.base import BackupContext
from drkit.sources.secrets import SecretsSource


class FakeRestic:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []

    def backup_paths(self, paths: Sequence[str], tags: Sequence[str]) -> BackupSummary:
        self.calls.append((list(paths), list(tags)))
        return BackupSummary("snap1", 99, 99)


def project(path: Path) -> Project:
    return Project.model_validate(
        {
            "name": "shop",
            "path": str(path),
            "git": {"remote": "git@example.com:shop.git"},
            "compose": {"files": ["compose.yaml"]},
            "secrets": [".env", "keys/jwt.pem"],
        }
    )


def ctx(restic: FakeRestic) -> BackupContext:
    return BackupContext(restic=cast(Restic, restic), run_id="r1", host="prod-01")


def test_backs_up_every_secret_in_one_snapshot(tmp_path: Path) -> None:
    (tmp_path / "keys").mkdir()
    (tmp_path / ".env").write_text("A=1\n")
    (tmp_path / "keys" / "jwt.pem").write_text("key\n")
    restic = FakeRestic()

    item = SecretsSource(project(tmp_path)).backup(ctx(restic))

    assert (item.status, item.snapshot, item.bytes, item.label) == (
        "ok",
        "snap1",
        99,
        "shop/secrets",
    )
    paths, tags = restic.calls[0]
    assert paths == [str(tmp_path / ".env"), str(tmp_path / "keys/jwt.pem")]
    assert tags == ["drkit", "host:prod-01", "run:r1", "kind:secrets", "project:shop"]


def test_missing_secret_fails_the_item(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")
    restic = FakeRestic()
    item = SecretsSource(project(tmp_path)).backup(ctx(restic))
    assert item.status == "failed"
    assert item.error == f"secret file(s) not found: {tmp_path / 'keys/jwt.pem'}"
    assert restic.calls == []
