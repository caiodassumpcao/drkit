"""git through its CLI: checkouts owned by another user, private remotes over a deploy key."""

from __future__ import annotations

import shlex
from pathlib import Path

from drkit import runner
from drkit.errors import CommandError

GIT_TIMEOUT_S = 120.0
CLONE_TIMEOUT_S = 1800.0
REMOTE_TIMEOUT_S = 30.0

# git must fail instead of waiting for a password nobody will type.
NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def args(path: Path, *arguments: str) -> list[str]:
    """argv for git inside the checkout at ``path``.

    drkit runs as root on checkouts owned by a deploy user: without safe.directory, git
    refuses to read them ("dubious ownership").
    """
    return ["git", "-c", f"safe.directory={path}", "-C", str(path), *arguments]


def ssh_command(deploy_key: Path | None) -> str:
    """``core.sshCommand`` for remotes: never prompt, and use only the deploy key if set.

    ``accept-new`` trusts a host key on first contact, as a freshly rebuilt server has no
    ``known_hosts`` yet, and still rejects a changed key.
    """
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if deploy_key is not None:
        command += ["-i", str(deploy_key), "-o", "IdentitiesOnly=yes"]
    return shlex.join(command)


def remote_args(deploy_key: Path | None, *arguments: str) -> list[str]:
    # Passed with -c rather than GIT_SSH_COMMAND: environment values are treated as secrets
    # and redacted everywhere, which would hide the key path from diagnostics.
    return ["git", "-c", f"core.sshCommand={ssh_command(deploy_key)}", *arguments]


def check_remote(remote: str, deploy_key: Path | None) -> None:
    """Prove the remote can be read with the configured credentials (``git ls-remote``)."""
    result = runner.run(
        remote_args(deploy_key, "ls-remote", "--heads", remote),
        timeout=REMOTE_TIMEOUT_S,
        env=NO_PROMPT_ENV,
        check=False,
    )
    if not result.ok:
        raise CommandError(
            f"cannot read git remote {remote}: {runner.failure_detail(result, max_lines=2)}",
            hint=_remote_hint(deploy_key),
        )


def clone(remote: str, destination: Path, deploy_key: Path | None) -> None:
    """Clone without checking out a branch: the caller checks out the recorded commit."""
    result = runner.run(
        remote_args(deploy_key, "clone", "--quiet", "--no-checkout", remote, str(destination)),
        timeout=CLONE_TIMEOUT_S,
        env=NO_PROMPT_ENV,
        check=False,
    )
    if not result.ok:
        raise CommandError(
            f"git clone of {remote} failed: {runner.failure_detail(result, max_lines=2)}",
            hint=_remote_hint(deploy_key),
        )


def checkout(path: Path, sha: str) -> None:
    """Check out ``sha`` as a detached HEAD: the exact commit that was running."""
    result = runner.run(
        args(path, "checkout", "--quiet", "--detach", sha), timeout=GIT_TIMEOUT_S, check=False
    )
    if not result.ok:
        raise CommandError(
            f"cannot check out commit {sha[:12]}: {runner.failure_detail(result, max_lines=2)}",
            hint="the commit must still exist on the remote (it is lost if a branch was "
            "force-pushed over it); restore an older run with --run",
        )


def checkout_ref(path: Path, ref: str) -> None:
    """Check out a tag, branch or commit; a branch is also looked up on ``origin``."""
    result = None
    for candidate in (ref, f"origin/{ref}"):
        result = runner.run(
            args(path, "checkout", "--quiet", "--detach", candidate),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        if result.ok:
            return
    assert result is not None  # noqa: S101 - the loop ran
    raise CommandError(
        f"cannot check out {ref!r} in {path}: {runner.failure_detail(result, max_lines=2)}",
        hint="use a tag, branch or commit that exists in the repository",
    )


def _remote_hint(deploy_key: Path | None) -> str:
    if deploy_key is None:
        return "check git.remote and network access; private repositories need git.deploy_key"
    return (
        f"check git.remote, network access and that {deploy_key} is a deploy key "
        "with read access to the repository"
    )
