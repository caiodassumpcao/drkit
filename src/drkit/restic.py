"""restic wrapper. drkit only talks to the repository through these calls (ADR-0001)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from drkit import runner
from drkit.config import Repository, Retention
from drkit.errors import CommandError, CommandTimeoutError, ConfigError, DrkitError

# Generous defaults: a timeout exists to unblock a hung process, not to bound a large backup.
BACKUP_TIMEOUT_S = 6 * 3600.0
COMMAND_TIMEOUT_S = 600.0
# Opening the repository is quick when it works; waiting longer only delays a clear error.
PROBE_TIMEOUT_S = 60.0

# Documented restic exit codes (restic >= 0.17); 11 (locked) and 12 (wrong password) are
# mapped in Restic._error.
EXIT_INCOMPLETE = 3  # backup: some source files could not be read (a snapshot is still saved)
EXIT_REPOSITORY_NOT_FOUND = 10

# Snapshot IDs per `restic forget` call, to stay far below the kernel's argv size limit.
_FORGET_BATCH = 100


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    short_id: str
    time: datetime
    hostname: str
    tags: tuple[str, ...]
    paths: tuple[str, ...]

    def tag_value(self, key: str) -> str | None:
        """Value of a ``key:value`` tag, e.g. ``tag_value("run")`` → run ID."""
        prefix = f"{key}:"
        return next((t.removeprefix(prefix) for t in self.tags if t.startswith(prefix)), None)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Snapshot:
        return cls(
            id=data["id"],
            short_id=data.get("short_id", data["id"][:8]),
            time=datetime.fromisoformat(data["time"]),
            hostname=data.get("hostname", ""),
            tags=tuple(data.get("tags") or ()),
            paths=tuple(data.get("paths") or ()),
        )


@dataclass(frozen=True, slots=True)
class Node:
    """A file or directory inside a snapshot, as listed by ``restic ls``."""

    path: str
    type: str
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class BackupSummary:
    snapshot_id: str
    bytes_processed: int
    data_added: int


class Restic:
    """A restic repository, scoped to one logical host."""

    def __init__(self, repository: Repository, *, host: str, env: Mapping[str, str]) -> None:
        self.url = repository.url
        self.host = host
        self.probe_timeout_s = PROBE_TIMEOUT_S
        self.env: dict[str, str] = {
            **env,
            "RESTIC_REPOSITORY": repository.url,
            "RESTIC_PASSWORD_FILE": str(repository.password_file),
        }

    # ------------------------------------------------------------------ repository

    def exists(self) -> bool:
        """True if the repository is initialized and the password is correct."""
        try:
            result = self._run(["cat", "config"], check=False, timeout=self.probe_timeout_s)
        except CommandTimeoutError:
            # restic treats an S3 "bucket does not exist" answer as transient and retries it
            # for ~15 minutes; a missing prefix inside an existing bucket returns 10 at once.
            raise CommandError(
                f"restic repository {runner.redact(self.url)} did not answer within "
                f"{self.probe_timeout_s:g}s",
                hint="check that the storage is reachable and the bucket exists "
                "(`drkit init` creates it); run with --verbose to see restic's retries",
            ) from None
        if result.ok:
            return True
        if result.returncode == EXIT_REPOSITORY_NOT_FOUND:
            return False
        raise self._error(result)

    def init(self) -> bool:
        """Initialize the repository (creating the bucket if needed).

        Returns False if it already existed. ``restic init`` is called directly rather than
        after :meth:`exists`, which would stall on a bucket that does not exist yet.
        """
        result = self._run(["init"], check=False)
        if result.ok:
            return True
        if "already initialized" in result.stderr or "already exists" in result.stderr:
            return False
        raise self._error(result)

    def ensure_ready(self) -> None:
        """Fail early, with an actionable error, if the repository cannot be used."""
        if not self.exists():
            raise ConfigError(
                f"restic repository {runner.redact(self.url)} is not initialized",
                hint="run `drkit init`",
            )

    # ------------------------------------------------------------------ backup

    def backup_stdin_command(self, filename: str, tags: Sequence[str]) -> list[str]:
        """argv for ``restic backup --stdin``, to be fed by :func:`runner.pipe`."""
        return [
            "restic",
            "backup",
            "--stdin",
            "--stdin-filename",
            filename,
            *self._backup_flags(tags),
        ]

    def backup_bytes(self, data: str, filename: str, tags: Sequence[str]) -> BackupSummary:
        """Store an in-memory document (e.g. the run manifest) as its own snapshot."""
        result = self._run(
            self.backup_stdin_command(filename, tags)[1:], input=data, timeout=COMMAND_TIMEOUT_S
        )
        return parse_backup_summary(result.stdout)

    def backup_paths(
        self, paths: Sequence[str], tags: Sequence[str], *, excludes: Sequence[str] = ()
    ) -> BackupSummary:
        """Back up files and directories as one snapshot.

        A snapshot missing files is worse than none (it looks like a backup), so when restic
        could not read everything the snapshot it saved anyway is forgotten and CommandError
        is raised.
        """
        args = ["backup", *self._backup_flags(tags)]
        for pattern in excludes:
            args += ["--exclude", pattern]
        result = self._run([*args, "--", *paths], check=False, timeout=BACKUP_TIMEOUT_S)
        try:
            summary: BackupSummary | None = parse_backup_summary(result.stdout)
        except CommandError:
            summary = None
        if result.ok and summary is not None:
            return summary
        if summary is not None:
            self.forget([summary.snapshot_id])
        if result.returncode == EXIT_INCOMPLETE:
            errors = "; ".join(json_errors(result.stdout)[:3])
            raise CommandError(
                f"restic could not read every file (snapshot discarded): {runner.redact(errors)}",
                hint="drkit must run as root to read every file it backs up",
            )
        if result.ok:
            raise CommandError("restic backup did not report a snapshot")
        raise self._error(result)

    def _backup_flags(self, tags: Sequence[str]) -> list[str]:
        flags = ["--host", self.host, "--json", "--quiet"]
        for tag in tags:
            flags += ["--tag", tag]
        return flags

    # ------------------------------------------------------------------ queries

    def snapshots(self, *tag_groups: Sequence[str]) -> list[Snapshot]:
        """Snapshots of this host, oldest first.

        Each group is AND-ed (``a,b``); groups are OR-ed, as in restic's ``--tag``.
        """
        args = ["snapshots", "--json", "--host", self.host, *_tag_args(tag_groups)]
        result = self._run(args)
        data = json.loads(result.stdout or "[]")
        return sorted((Snapshot.from_json(s) for s in data), key=lambda s: s.time)

    def snapshot(self, snapshot_id: str) -> Snapshot:
        """One snapshot by ID (full or short)."""
        data = json.loads(self._run(["snapshots", "--json", snapshot_id]).stdout or "[]")
        if not data:
            raise CommandError(
                f"snapshot {snapshot_id[:8]} not found in the repository",
                hint="it may have been forgotten by retention or `restic forget`; pick another "
                "run with --run (see `drkit runs list`)",
            )
        return Snapshot.from_json(data[0])

    def ls(self, snapshot_id: str) -> list[Node]:
        """Every file and directory of a snapshot, with its original owner."""
        nodes = []
        for line in self._run(["ls", "--json", snapshot_id]).stdout.splitlines():
            data = json.loads(line)
            if data.get("struct_type") == "node":
                nodes.append(
                    Node(
                        path=data["path"],
                        type=data["type"],
                        uid=int(data.get("uid", 0)),
                        gid=int(data.get("gid", 0)),
                    )
                )
        return nodes

    def restore(self, snapshot_id: str, path: str, target: str, *, delete: bool = False) -> None:
        """Restore the contents of directory ``path`` of a snapshot into ``target``.

        Files are always overwritten. With ``delete``, files in ``target`` that are not in the
        snapshot are removed, so ``target`` ends up identical to the backed-up directory.
        restic restores owners and modes when it runs as root.
        """
        args = ["restore", f"{snapshot_id}:{path}", "--target", target]
        if delete:
            args.append("--delete")
        self._run(args, timeout=BACKUP_TIMEOUT_S)

    def dump(self, snapshot_id: str, path: str) -> str:
        return self._run(["dump", snapshot_id, path]).stdout

    def dump_command(self, snapshot_id: str, path: str) -> list[str]:
        """argv for ``restic dump``, to stream a file into :func:`runner.pipe`."""
        return ["restic", "dump", snapshot_id, path]

    def dump_into(
        self,
        snapshot_id: str,
        path: str,
        consumer: Sequence[str],
        *,
        label: str,
        consumer_env: Mapping[str, str] | None = None,
    ) -> None:
        """Stream file ``path`` of a snapshot into ``consumer``'s stdin, without a temp file.

        Raises CommandError when either side fails: a restore fed a truncated dump must not
        pass for a complete one.
        """
        result = runner.pipe(
            self.dump_command(snapshot_id, path),
            consumer,
            timeout=BACKUP_TIMEOUT_S,
            producer_env=self.env,
            consumer_env=consumer_env,
        )
        if not result.producer.ok:
            raise CommandError(
                f"restic dump of {path.lstrip('/')} failed with exit code "
                f"{result.producer.returncode}: "
                f"{runner.failure_detail(result.producer, max_lines=3)}"
            )
        if not result.consumer.ok:
            raise CommandError(
                f"{label} failed with exit code {result.consumer.returncode}: "
                f"{runner.failure_detail(result.consumer, max_lines=3)}"
            )

    # ------------------------------------------------------------------ maintenance

    def check(self, read_data_subset: str | None = None) -> None:
        """Verify the repository structure and, with ``read_data_subset``, part of the data."""
        args = ["check"]
        if read_data_subset:
            args.append(f"--read-data-subset={read_data_subset}")
        self._run(args, timeout=BACKUP_TIMEOUT_S)

    def prune(self) -> None:
        """Delete data no snapshot references any more (needs an exclusive lock)."""
        self._run(["prune"], timeout=BACKUP_TIMEOUT_S)

    # ------------------------------------------------------------------ retention

    def forget(self, snapshot_ids: Iterable[str]) -> int:
        """Forget snapshots by ID (data is reclaimed later by prune). Returns the count."""
        ids = list(dict.fromkeys(snapshot_ids))
        for start in range(0, len(ids), _FORGET_BATCH):
            self._run(["forget", *ids[start : start + _FORGET_BATCH]])
        return len(ids)

    def retention_plan(
        self, retention: Retention, tags: Sequence[str]
    ) -> tuple[list[Snapshot], list[Snapshot]]:
        """Apply ``retention`` to the snapshots matching ``tags`` (dry run).

        Returns ``(keep, remove)``. Snapshots are grouped by host only, so tags that differ
        per snapshot (such as ``run:<id>``) do not split them into groups of one.
        """
        args = ["forget", "--dry-run", "--json", "--host", self.host, "--group-by", "host"]
        args += _tag_args([tags])
        args += retention_args(retention)
        groups = json.loads(self._run(args).stdout or "[]")
        keep = [Snapshot.from_json(s) for g in groups for s in g.get("keep") or ()]
        remove = [Snapshot.from_json(s) for g in groups for s in g.get("remove") or ()]
        return keep, remove

    # ------------------------------------------------------------------ internals

    def _run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        input: str | None = None,
        timeout: float = COMMAND_TIMEOUT_S,
    ) -> runner.CommandResult:
        result = runner.run(
            ["restic", *args], timeout=timeout, env=self.env, input=input, check=False
        )
        if check and not result.ok:
            raise self._error(result)
        return result

    def _error(self, result: runner.CommandResult) -> DrkitError:
        url = runner.redact(self.url)
        detail = runner.failure_detail(result, max_lines=2)
        match result.returncode:
            case 10:
                return ConfigError(
                    f"restic repository {url} does not exist", hint="run `drkit init`"
                )
            case 12:
                return ConfigError(
                    f"wrong password for restic repository {url}",
                    hint="repository.password_file must hold the key used at `drkit init`",
                )
            case 11:
                return CommandError(
                    f"restic repository {url} is locked: {detail}",
                    hint="wait for the other restic process; remove stale locks with "
                    "`restic unlock`",
                )
        return CommandError(
            f"restic {result.args[1]} failed with exit code {result.returncode}: {detail}",
            hint="check repository.url, network access and the credentials in the env file "
            "(`drkit doctor`)",
        )


def parse_backup_summary(stdout: str) -> BackupSummary:
    """Extract the summary message from ``restic backup --json`` output."""
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("message_type") == "summary":
            return BackupSummary(
                snapshot_id=message["snapshot_id"],
                bytes_processed=int(message.get("total_bytes_processed", 0)),
                data_added=int(message.get("data_added", 0)),
            )
    raise CommandError("restic backup did not report a summary (no snapshot was created)")


def json_errors(stdout: str) -> list[str]:
    """Messages of the ``error`` lines of ``restic backup --json`` output."""
    errors: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("message_type") == "error":
            errors.append(str(message.get("error", {}).get("message", "")))
    return errors


def retention_args(retention: Retention) -> list[str]:
    args: list[str] = []
    for flag, value in (
        ("--keep-daily", retention.daily),
        ("--keep-weekly", retention.weekly),
        ("--keep-monthly", retention.monthly),
    ):
        if value is not None:
            args += [flag, str(value)]
    return args


def _tag_args(tag_groups: Iterable[Sequence[str]]) -> list[str]:
    args: list[str] = []
    for group in tag_groups:
        if group:
            args += ["--tag", ",".join(group)]
    return args
