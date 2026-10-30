"""Restores against real Docker and MinIO: databases in place, and a destroyed project rebuilt."""

import json
import secrets
import shutil
import socket
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from drkit import docker
from drkit.sources import volume as volume_module
from tests.integration.conftest import Stack
from tests.integration.test_database_backup import MARIADB, MYSQL, POSTGRES

pytestmark = pytest.mark.integration

DEMO = Path(__file__).with_name("demo")


# --------------------------------------------------------------------------- restore db


def test_restore_db_puts_back_the_backed_up_data(make_drkit: Any, stack: Stack) -> None:
    drkit = make_drkit(databases={"shop": [POSTGRES, MYSQL], "blog": [MARIADB]})
    drkit("init")
    assert drkit("backup", "--only", "db").exit_code == 0
    try:
        # What happens after the backup, and is undone by the restore.
        stack.sql("db", "INSERT INTO orders VALUES (2, 'drkit-after-backup');")
        stack.sql("mysql", "INSERT INTO notes VALUES (2, 'drkit-after-backup');")
        stack.sql("mariadb", "DROP TABLE posts;")

        dry_run = drkit("restore", "db", "shop", "--dry-run")
        assert dry_run.exit_code == 0, dry_run.output
        assert "4. Sanity check" in dry_run.stdout
        assert drkit.restic().snapshots(["drkit", "kind:pre-restore"]) == []

        result = drkit("--json", "restore", "db", "shop", "--yes")
        assert result.exit_code == 0, result.output
        blog = drkit("restore", "db", "blog", "--yes", "--no-safety-backup")
        assert blog.exit_code == 0, blog.output

        assert stack.sql("db", "SELECT id, item FROM orders").split() == ["1|drkit-seed-postgres"]
        assert stack.sql("mysql", "SELECT id, body FROM notes").splitlines() == [
            "1\tdrkit-seed-mysql"
        ]
        assert stack.sql("mariadb", "SELECT title FROM posts").strip() == "drkit-seed-mariadb"
    finally:
        _reseed(stack)

    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    assert [step["status"] for step in report["steps"]] == ["ok"] * 4
    assert Path(report["report_path"]).is_file()

    # The safety dumps hold the state the restore replaced, outside of any run.
    restic = drkit.restic()
    safety = restic.snapshots(["drkit", "kind:pre-restore"])
    assert {(s.tag_value("db"), s.tag_value("restore")) for s in safety} == {
        ("shop", report["restore_id"]),
        ("legacy", report["restore_id"]),
    }
    assert all(s.tag_value("run") is None for s in safety)
    [legacy] = [s for s in safety if s.tag_value("db") == "legacy"]
    assert "drkit-after-backup" in restic.dump(legacy.id, "/shop/db/legacy.sql")


def _reseed(stack: Stack) -> None:
    """Put the shared stack back in its seeded state, whatever happened in the test."""
    stack.sql("db", "DELETE FROM orders WHERE id <> 1;")
    stack.sql("mysql", "DELETE FROM notes WHERE id <> 1;")
    stack.sql("mariadb", "CREATE TABLE IF NOT EXISTS posts (id int PRIMARY KEY, title text);")
    stack.sql("mariadb", "INSERT IGNORE INTO posts VALUES (1, 'drkit-seed-mariadb');")


# --------------------------------------------------------------------------- restore project


@dataclass
class Demo:
    """A deployed project: a clone of ``remote`` at ``path``, running as ``name``."""

    name: str
    path: Path
    remote: Path
    port: int

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/www/health"

    def compose(self, *args: str) -> str:
        return subprocess.run(
            [
                *("docker", "compose", "-p", self.name),
                *("--project-directory", str(self.path), "-f", str(self.path / "compose.yaml")),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def sql(self, statement: str) -> str:
        return self.compose(
            "exec", "-T", "db", "psql", "-U", "app", "-d", "shop", "-XtAc", statement
        ).strip()

    def fixtures(self) -> str:
        """Row count and a checksum of every row: equal fixtures mean identical data."""
        return self.sql("SELECT count(*), md5(string_agg(payload, ',' ORDER BY id)) FROM items")

    def uploads(self) -> dict[str, str]:
        """Checksum of every uploaded file, by path."""
        output = self.compose(
            "exec", "-T", "web", "sh", "-c", "cd /srv/uploads && find . -type f | xargs md5sum"
        )
        return {path: digest for digest, path in (line.split() for line in output.splitlines())}

    def destroy(self) -> None:
        """The disaster: containers, volumes and the project directory are gone."""
        subprocess.run(
            ["docker", "compose", "-p", self.name, "down", "--volumes", "--remove-orphans"],
            check=True,
            capture_output=True,
        )
        shutil.rmtree(self.path)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def demo(tmp_path: Path, stack: Stack) -> Iterator[Demo]:
    work = tmp_path / "work"
    (work / "www").mkdir(parents=True)
    shutil.copy(DEMO / "compose.yaml", work / "compose.yaml")
    (work / "www" / "health").write_text("ok\n")
    (work / "www" / "index.html").write_text("v1\n")
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "dev@example.com")
    git(work, "config", "user.name", "Dev")
    git(work, "config", "commit.gpgsign", "false")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "release v1")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote)], check=True)

    path = tmp_path / "srv" / "shop"
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True)
    (path / "www" / "index.html").write_text("v1 + hotfix\n")  # deployed by hand, uncommitted
    env = path / ".env"
    env.write_text(f"DB_PASSWORD=drkit-test-only\nWEB_PORT={free_port()}\n")
    env.chmod(0o600)

    demo = Demo(
        name=f"drkit-demo-{secrets.token_hex(3)}",
        path=path,
        remote=remote,
        port=int(env.read_text().split("WEB_PORT=")[1]),
    )
    try:
        demo.compose("up", "--detach", "--wait", "--wait-timeout", "180")
        demo.sql(
            "CREATE TABLE items (id int PRIMARY KEY, payload text); "
            "INSERT INTO items SELECT i, md5(i::text) FROM generate_series(1, 500) AS i;"
        )
        demo.compose(
            "exec",
            "-T",
            "web",
            "sh",
            "-c",
            "mkdir -p /srv/uploads/nested && echo hello > /srv/uploads/a.txt && "
            "head -c 65536 /dev/urandom > /srv/uploads/nested/b.bin",
        )
        yield demo
    finally:
        subprocess.run(
            ["docker", "compose", "-p", demo.name, "down", "--volumes", "--remove-orphans"],
            check=False,
            capture_output=True,
        )


@pytest.mark.parametrize("with_volume", [True, False], ids=["with-volume", "without-volume"])
def test_restore_project_rebuilds_a_destroyed_project(
    make_drkit: Any, demo: Demo, with_volume: bool
) -> None:
    if with_volume:
        uploads = docker.volume_mountpoint(docker.compose_volume(demo.name, "uploads"))
        if not volume_module.readable(uploads):
            pytest.skip(f"volume data at {uploads} is not reachable from this host (run as root)")
    drkit = make_drkit(
        databases={"shop": [{"engine": "postgres", "service": "db", "database": "shop"}]},
        projects={
            "shop": {
                "path": str(demo.path),
                "git": {"remote": str(demo.remote)},
                "compose": {"project_name": demo.name, "files": ["compose.yaml"]},
                "secrets": [".env"],
                "volumes": [{"name": "uploads"}] if with_volume else [],
                "hooks": {"post_restore": ["touch restored-by-hook"]},
                "healthchecks": [{"url": demo.health_url, "timeout": 60}],
            }
        },
    )
    drkit("init")
    backup = drkit("backup", "--project", "shop")
    assert backup.exit_code == 0, backup.output
    sha = git(demo.path, "rev-parse", "HEAD")
    rows, files = demo.fixtures(), demo.uploads()

    demo.destroy()
    result = drkit("--json", "restore", "project", "shop", "--yes")

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert [(s["number"], s["status"]) for s in report["steps"]] == [
        (n, "ok") for n in range(1, 11)
    ]
    assert report["steps"][0]["detail"] == "nothing to save: the project does not exist here"
    assert Path(report["report_path"]).is_file()

    # Identical data, the same code, the secrets back in place, and a healthy service.
    assert demo.fixtures() == rows
    if with_volume:
        assert demo.uploads() == files
    assert git(demo.path, "rev-parse", "HEAD") == sha
    assert (demo.path / "www" / "index.html").read_text() == "v1 + hotfix\n"
    assert (demo.path / ".env").stat().st_mode & 0o777 == 0o600
    assert (demo.path / "restored-by-hook").exists()
    assert httpx.get(demo.health_url, timeout=10).text == "ok\n"

    # Restoring over the running project first saves what exists of it, outside of any run.
    again = drkit("--json", "restore", "project", "shop", "--yes")
    assert again.exit_code == 0, again.output
    second = json.loads(again.stdout)
    saved = "4 item(s) saved" if with_volume else "3 item(s) saved"
    assert second["steps"][0]["detail"].startswith(saved)
    safety = drkit.restic().snapshots(["drkit", "kind:pre-restore"])
    assert {s.tag_value("item") for s in safety} == (
        {"db", "volume", "secrets", "project"} if with_volume else {"db", "secrets", "project"}
    )
    assert {s.tag_value("restore") for s in safety} == {second["restore_id"]}
    assert all(s.tag_value("run") is None for s in safety)
    assert demo.fixtures() == rows
