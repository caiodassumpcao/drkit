import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from drkit import docker
from drkit.config import Project
from drkit.errors import CommandError
from drkit.restic import BackupSummary, Restic
from drkit.sources import project as project_module
from drkit.sources.base import BackupContext
from drkit.sources.project import ProjectSource

REMOTE = "git@example.com:shop.git"


class FakeRestic:
    def __init__(self) -> None:
        self.paths: list[list[str]] = []
        self.forgotten: list[str] = []

    def backup_paths(self, paths: Sequence[str], tags: Sequence[str]) -> BackupSummary:
        self.paths.append(list(paths))
        return BackupSummary("files1", 100, 100)

    def forget(self, ids: Sequence[str]) -> int:
        self.forgotten += ids
        return len(ids)


class FakeStream:
    """Stands in for stream_dump: runs the producer and keeps its output."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.output = b""

    def __call__(self, ctx: BackupContext, producer: Sequence[str], **kwargs: Any) -> BackupSummary:
        self.calls.append({"producer": list(producer), **kwargs})
        if self.fail:
            raise CommandError("git diff failed with exit code 128")
        self.output = subprocess.run(producer, check=True, capture_output=True).stdout
        return BackupSummary("patch1", len(self.output), 1)


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "remote", "add", "origin", REMOTE)
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "app.py").write_text("print('v1')\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@pytest.fixture(autouse=True)
def images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docker, "project_images", lambda project: {"app": "ghcr.io/acme/app:1@sha256:a"}
    )


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> FakeStream:
    fake = FakeStream()
    monkeypatch.setattr(project_module, "stream_dump", fake)
    return fake


def source(path: Path, **compose: Any) -> ProjectSource:
    project = Project.model_validate(
        {
            "name": "shop",
            "path": str(path),
            "git": {"remote": REMOTE},
            "compose": {"files": ["compose.yaml"], **compose},
        }
    )
    return ProjectSource(project)


def backup(src: ProjectSource, restic: FakeRestic) -> Any:
    return src.backup(BackupContext(restic=cast(Restic, restic), run_id="r1", host="h"))


def head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_clean_checkout(checkout: Path, stream: FakeStream) -> None:
    restic = FakeRestic()
    src = source(checkout)

    item = backup(src, restic)

    assert (item.status, item.snapshot, item.label) == ("ok", "files1", "shop/project")
    assert restic.paths == [[str(checkout / "compose.yaml")]]
    assert stream.calls == []
    assert src.state is not None
    assert src.state.sha == head(checkout)
    assert src.state.remote == REMOTE
    assert src.state.dirty is False
    assert src.state.patch_snapshot is None
    assert src.state.images == {"app": "ghcr.io/acme/app:1@sha256:a"}


def test_uncommitted_changes_are_stored_as_a_patch(checkout: Path, stream: FakeStream) -> None:
    (checkout / "app.py").write_text("print('hotfix')\n")
    (checkout / "notes.txt").write_text("untracked\n")
    restic = FakeRestic()
    src = source(checkout)

    item = backup(src, restic)

    assert item.status == "ok"
    assert item.bytes == 100 + len(stream.output)
    assert stream.calls[0]["producer"][-3:] == ["diff", "HEAD", "--binary"]
    assert stream.calls[0]["filename"] == "shop/project/dirty.patch"
    assert b"+print('hotfix')" in stream.output
    assert src.state is not None
    assert (src.state.dirty, src.state.patch_snapshot) == (True, "patch1")


def test_only_untracked_files_need_no_patch(checkout: Path, stream: FakeStream) -> None:
    (checkout / "notes.txt").write_text("untracked\n")
    src = source(checkout)
    assert backup(src, FakeRestic()).status == "ok"
    assert stream.calls == []
    assert src.state is not None
    assert src.state.dirty is True


def test_failed_patch_drops_the_compose_snapshot(checkout: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(project_module, "stream_dump", FakeStream(fail=True))
    (checkout / "app.py").write_text("print('hotfix')\n")
    restic = FakeRestic()
    src = source(checkout)

    item = backup(src, restic)

    assert item.status == "failed"
    assert restic.forgotten == ["files1"]
    assert src.state is None


def test_not_a_git_checkout(tmp_path: Path) -> None:
    item = backup(source(tmp_path), FakeRestic())
    assert item.status == "failed"
    assert item.error.startswith(f"{tmp_path} is not a git checkout with a commit:")


def test_missing_compose_file(checkout: Path, stream: FakeStream) -> None:
    item = backup(source(checkout, files=["compose.yaml", "compose.prod.yaml"]), FakeRestic())
    assert item.error == f"compose file(s) not found: {checkout / 'compose.prod.yaml'}"


def test_git_runs_with_safe_directory(checkout: Path) -> None:
    assert source(checkout).git_args("status")[:5] == [
        "git",
        "-c",
        f"safe.directory={checkout}",
        "-C",
        str(checkout),
    ]
