import subprocess
from pathlib import Path

import pytest

from drkit import git, runner
from drkit.errors import CommandError


def run_git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    """A bare repository with two commits; returns its path and the first commit."""
    work = tmp_path / "work"
    work.mkdir()
    run_git("-C", str(work), "init", "-q", "-b", "main")
    run_git("-C", str(work), "config", "user.email", "dev@example.com")
    run_git("-C", str(work), "config", "user.name", "Dev")
    run_git("-C", str(work), "config", "commit.gpgsign", "false")
    (work / "app.py").write_text("v1\n")
    run_git("-C", str(work), "add", ".")
    run_git("-C", str(work), "commit", "-q", "-m", "v1")
    first = run_git("-C", str(work), "rev-parse", "HEAD").strip()
    (work / "app.py").write_text("v2\n")
    run_git("-C", str(work), "commit", "-q", "-am", "v2")
    bare = tmp_path / "remote.git"
    run_git("clone", "-q", "--bare", str(work), str(bare))
    return bare, first


def test_args_trust_the_checkout() -> None:
    assert git.args(Path("/srv/shop"), "status") == [
        "git",
        "-c",
        "safe.directory=/srv/shop",
        "-C",
        "/srv/shop",
        "status",
    ]


def test_ssh_command() -> None:
    assert git.ssh_command(None) == "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    assert git.ssh_command(Path("/etc/drkit/keys/shop")) == (
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        "-i /etc/drkit/keys/shop -o IdentitiesOnly=yes"
    )


def test_remote_commands_never_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> runner.CommandResult:
        calls.append({"args": args, **kwargs})
        return runner.CommandResult(tuple(args), 0, "", "", 0.0)

    monkeypatch.setattr(runner, "run", fake_run)
    git.check_remote("git@example.com:shop.git", Path("/k"))
    assert calls[0]["args"] == [
        "git",
        "-c",
        f"core.sshCommand={git.ssh_command(Path('/k'))}",
        "ls-remote",
        "--heads",
        "git@example.com:shop.git",
    ]
    assert calls[0]["env"] == {"GIT_TERMINAL_PROMPT": "0"}


def test_clone_and_checkout_a_recorded_commit(remote: tuple[Path, str], tmp_path: Path) -> None:
    bare, first = remote
    git.check_remote(str(bare), None)
    destination = tmp_path / "restored"

    git.clone(str(bare), destination, None)
    git.checkout(destination, first)

    assert (destination / "app.py").read_text() == "v1\n"
    assert run_git("-C", str(destination), "rev-parse", "HEAD").strip() == first


def test_unreadable_remote(tmp_path: Path) -> None:
    with pytest.raises(CommandError) as exc_info:
        git.check_remote(str(tmp_path / "missing.git"), Path("/etc/drkit/keys/shop"))
    assert exc_info.value.message.startswith(f"cannot read git remote {tmp_path / 'missing.git'}:")
    assert exc_info.value.hint is not None
    assert "/etc/drkit/keys/shop is a deploy key" in exc_info.value.hint


def test_failed_clone(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match=r"git clone of .* failed") as exc_info:
        git.clone(str(tmp_path / "missing.git"), tmp_path / "dest", None)
    assert exc_info.value.hint is not None
    assert "git.deploy_key" in exc_info.value.hint


def test_checkout_of_a_lost_commit(remote: tuple[Path, str], tmp_path: Path) -> None:
    bare, _ = remote
    destination = tmp_path / "restored"
    git.clone(str(bare), destination, None)
    with pytest.raises(CommandError, match="cannot check out commit 0123456789ab") as exc_info:
        git.checkout(destination, "0123456789ab" * 3 + "0123")
    assert exc_info.value.hint is not None
    assert "--run" in exc_info.value.hint
