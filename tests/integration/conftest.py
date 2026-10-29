"""Fixtures for integration tests: a disposable Docker Compose stack and drkit configurations.

These tests need Docker (with Compose v2) and restic on the host. They are skipped otherwise.
"""

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner, Result

from drkit import cli, runner
from drkit.config import LoadedConfig, load_config
from drkit.orchestrator import restic_for
from drkit.restic import Restic

COMPOSE_FILE = Path(__file__).with_name("compose.yaml")
MINIO_USER = "drkit-test"
MINIO_PASSWORD = "drkit-test-only"  # matches compose.yaml
HOST = "it-host"
BUCKET = "drkit-it"


@dataclass(frozen=True)
class Stack:
    project: str
    s3_endpoint: str

    def compose(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],
            check=True,
            capture_output=True,
            text=True,
            **kwargs,
        )

    def sql(self, service: str, statement: str) -> str:
        """Run SQL in a service as the application user; returns stdout."""
        clients = {
            "db": ["psql", "-U", "app", "-d", "shop", "-XtAc", statement],
            "mysql": ["mysql", "-uapp", f"-p{MINIO_PASSWORD}", "legacy", "-N", "-e", statement],
            "mariadb": ["mariadb", "-uapp", f"-p{MINIO_PASSWORD}", "blog", "-N", "-e", statement],
        }
        return self.compose("exec", "-T", service, *clients[service]).stdout


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    if shutil.which("docker") is None or shutil.which("restic") is None:
        pytest.skip("integration tests need docker and restic")
    project = f"drkit-it-{secrets.token_hex(3)}"
    up = Stack(project=project, s3_endpoint="")
    try:
        up.compose("up", "-d", "--wait", "--wait-timeout", "240")
        port = up.compose("port", "minio", "9000").stdout.strip().rsplit(":", 1)[1]
        stack = Stack(project=project, s3_endpoint=f"http://127.0.0.1:{port}")
        _wait_for(f"{stack.s3_endpoint}/minio/health/live")
        _create_bucket(stack, tmp_path_factory.mktemp("bucket"))
        _seed(stack)
        yield stack
    finally:
        up.compose("down", "--volumes", "--remove-orphans")


def _wait_for(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(url, timeout=2).is_success:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"{url} did not become ready")
        time.sleep(0.5)


def _create_bucket(stack: Stack, workdir: Path) -> None:
    # Tests use a prefix of this bucket: restic answers "does not exist" for a missing prefix
    # at once, but retries a missing bucket for minutes.
    password = _private(workdir / "pw", "bootstrap")
    subprocess.run(
        ["restic", "init", "--repo", f"s3:{stack.s3_endpoint}/{BUCKET}/bootstrap"],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "AWS_ACCESS_KEY_ID": MINIO_USER,
            "AWS_SECRET_ACCESS_KEY": MINIO_PASSWORD,
            "RESTIC_PASSWORD_FILE": str(password),
        },
    )


def _seed(stack: Stack) -> None:
    stack.sql("db", "CREATE TABLE orders (id int PRIMARY KEY, item text);")
    stack.sql("db", "INSERT INTO orders VALUES (1, 'drkit-seed-postgres');")
    stack.sql("mysql", "CREATE TABLE notes (id int PRIMARY KEY, body text);")
    stack.sql("mysql", "INSERT INTO notes VALUES (1, 'drkit-seed-mysql');")
    stack.sql("mariadb", "CREATE TABLE posts (id int PRIMARY KEY, title text);")
    stack.sql("mariadb", "INSERT INTO posts VALUES (1, 'drkit-seed-mariadb');")


@dataclass
class Webhook:
    """Local HTTP endpoint standing in for Slack: records every JSON body it receives."""

    url: str
    messages: list[str] = field(default_factory=list)


@pytest.fixture
def webhook() -> Iterator[Webhook]:
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body)["text"])
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Webhook(url=f"http://127.0.0.1:{server.server_port}/hook", messages=received)
    finally:
        server.shutdown()


@dataclass
class Drkit:
    """A drkit installation in a temporary directory, driven through the real CLI."""

    config: Path
    state_dir: Path

    def __call__(self, *args: str) -> Result:
        return CliRunner().invoke(cli.app, ["--config", str(self.config), *args])

    def loaded(self) -> LoadedConfig:
        return load_config(self.config)

    def restic(self) -> Restic:
        loaded = self.loaded()
        return restic_for(loaded.manifest, dict(loaded.env))


@pytest.fixture
def make_drkit(tmp_path: Path, stack: Stack, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DRKIT_LOCK_FILE", str(tmp_path / "drkit.lock"))

    def make(
        *,
        databases: dict[str, list[dict[str, str]]],
        retention: dict[str, int] | None = None,
        webhook_url: str | None = None,
        bucket: str | None = None,
    ) -> Drkit:
        location = bucket or f"{BUCKET}/{secrets.token_hex(4)}"
        password = _private(tmp_path / "restic.pass", secrets.token_urlsafe(24))
        env = _private(
            tmp_path / "env",
            f"AWS_ACCESS_KEY_ID={MINIO_USER}\nAWS_SECRET_ACCESS_KEY={MINIO_PASSWORD}\n",
        )
        manifest: dict[str, Any] = {
            "version": 1,
            "host": HOST,
            "repository": {
                "url": f"s3:{stack.s3_endpoint}/{location}",
                "password_file": str(password),
                "env_file": str(env),
            },
            "retention": retention or {"daily": 7},
            "projects": [
                {
                    "name": name,
                    "path": f"/srv/{name}",
                    "git": {"remote": f"git@example.com:{name}.git"},
                    "compose": {"project_name": stack.project, "files": ["compose.yaml"]},
                    "databases": dbs,
                }
                for name, dbs in databases.items()
            ],
        }
        if webhook_url:
            manifest["notifications"] = {
                "on": ["failure"],
                "webhook": {"url": webhook_url},
            }
        config = _private(tmp_path / "dr.yaml", yaml.safe_dump(manifest, sort_keys=False))
        return Drkit(config=config, state_dir=tmp_path / "state")

    yield make
    runner.redactor.clear()


def _private(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o600)
    return path
