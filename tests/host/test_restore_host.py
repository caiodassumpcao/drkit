"""Host recovery on a disposable machine: back up, destroy, ``drkit restore host``, compare.

DESTRUCTIVE: it writes /etc/drkit, /etc/nginx, /etc/letsencrypt, /etc/systemd/system and /srv,
and runs a real hardening playbook (server-hardening-kit) on this machine. It only runs as
root with DRKIT_HOST_TEST=1, as the CI job "Host recovery" does on a throwaway runner, after
``scripts/bootstrap.sh`` installed drkit and its dependencies.
"""

import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.integration.conftest import COMPOSE_FILE, MINIO_PASSWORD, MINIO_USER

pytestmark = [
    pytest.mark.host,
    pytest.mark.skipif(
        os.environ.get("DRKIT_HOST_TEST") != "1" or os.geteuid() != 0,
        reason="destructive: runs only as root with DRKIT_HOST_TEST=1, on a disposable host",
    ),
]

DEMO = Path(__file__).parents[1] / "integration" / "demo"
PROJECT = Path("/srv/demo")
REMOTE = Path("/srv/git/demo.git")
ETC = Path("/etc/drkit")
SITE = Path("/etc/nginx/sites-available/demo")
SITE_LINK = Path("/etc/nginx/sites-enabled/demo")
LIVE = Path("/etc/letsencrypt/live/demo.test")
UNIT = Path("/etc/systemd/system/drkit-demo-report.service")
PLAYBOOK = "https://github.com/caiodassumpcao/server-hardening-kit.git"
WEB_PORT = 8080

NGINX_SITE = f"""
server {{
    listen 80 default_server;
    listen 443 ssl default_server;
    server_name demo.test;
    ssl_certificate {LIVE}/fullchain.pem;
    ssl_certificate_key {LIVE}/privkey.pem;
    location / {{
        proxy_pass http://127.0.0.1:{WEB_PORT};
    }}
}}
"""


def sh(*args: str, cwd: Path | None = None, timeout: float = 600) -> str:
    return subprocess.run(
        list(args), check=True, capture_output=True, text=True, cwd=cwd, timeout=timeout
    ).stdout


def drkit(*args: str) -> subprocess.CompletedProcess[str]:
    """The drkit installed by bootstrap.sh, as an operator would run it."""
    return subprocess.run(
        ["drkit", *args], capture_output=True, text=True, timeout=3600, check=False
    )


def compose(*args: str) -> str:
    return sh(
        "docker", "compose", "-p", "demo", "--project-directory", str(PROJECT),
        "-f", str(PROJECT / "compose.yaml"), *args,
    )  # fmt: skip


def sql(statement: str) -> str:
    return compose("exec", "-T", "db", "psql", "-U", "app", "-d", "shop", "-XtAc", statement)


def fixtures() -> dict[str, Any]:
    """Everything that must come back identical."""
    uploads = compose(
        "exec", "-T", "web", "sh", "-c", "cd /srv/uploads && find . -type f | xargs md5sum"
    )
    return {
        "rows": sql("SELECT count(*), md5(string_agg(payload, ',' ORDER BY id)) FROM items"),
        "uploads": sorted(uploads.splitlines()),
        "site": SITE.read_text(),
        "certificate": (LIVE / "fullchain.pem").read_bytes(),
        "unit": UNIT.read_text(),
        "secret": (PROJECT / ".env").read_text(),
    }


@pytest.fixture(scope="module")
def storage() -> Iterator[str]:
    """Off-site storage: a MinIO container, which survives the disaster."""
    project = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", "drkit-host"]
    sh(*project, "up", "--detach", "--wait", "minio")
    try:
        port = sh(*project, "port", "minio", "9000").strip().rsplit(":", 1)[1]
        yield f"s3:http://127.0.0.1:{port}/drkit-host/{secrets.token_hex(4)}"
    finally:
        subprocess.run([*project, "down", "--volumes"], check=False, capture_output=True)


def deploy() -> None:
    """The server before the disaster: a project deployed from git, behind nginx with TLS."""
    work = Path(sh("mktemp", "-d").strip())
    (work / "www").mkdir()
    shutil.copy(DEMO / "compose.yaml", work / "compose.yaml")
    (work / "www" / "health").write_text("ok\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.email=ops@example.com", "-c", "user.name=Ops", "add", "."],
        ["-c", "user.email=ops@example.com", "-c", "user.name=Ops", "commit", "-q", "-m", "v1"],
    ):
        sh("git", *args, cwd=work)
    REMOTE.parent.mkdir(parents=True, exist_ok=True)
    sh("git", "clone", "-q", "--bare", str(work), str(REMOTE))
    sh("git", "clone", "-q", str(REMOTE), str(PROJECT))
    env = PROJECT / ".env"
    env.write_text(f"DB_PASSWORD={secrets.token_urlsafe(16)}\nWEB_PORT={WEB_PORT}\n")
    env.chmod(0o600)
    compose("up", "--detach", "--wait", "--wait-timeout", "180")
    sql(
        "CREATE TABLE items (id int PRIMARY KEY, payload text); "
        "INSERT INTO items SELECT i, md5(i::text) FROM generate_series(1, 1000) AS i;"
    )
    compose(
        "exec", "-T", "web", "sh", "-c",
        "mkdir -p /srv/uploads/2026 && echo hello > /srv/uploads/a.txt && "
        "head -c 262144 /dev/urandom > /srv/uploads/2026/photo.jpg",
    )  # fmt: skip

    LIVE.mkdir(parents=True)
    sh(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
        "-subj", "/CN=demo.test", "-keyout", str(LIVE / "privkey.pem"),
        "-out", str(LIVE / "fullchain.pem"),
    )  # fmt: skip
    SITE.write_text(NGINX_SITE)
    Path("/etc/nginx/sites-enabled/default").unlink(missing_ok=True)
    SITE_LINK.symlink_to(SITE)
    sh("nginx", "-t")
    sh("systemctl", "reload-or-restart", "nginx")
    UNIT.write_text("[Unit]\nDescription=drkit demo report\n[Service]\nType=oneshot\n"
                    "ExecStart=/bin/true\n")  # fmt: skip


def configure(repository: str) -> None:
    """/etc/drkit as the operator puts it back from the password manager."""
    ETC.mkdir(mode=0o700, exist_ok=True)
    password = ETC / "restic.pass"
    password.write_text(secrets.token_urlsafe(32) + "\n")
    env = ETC / "env"
    env.write_text(f"AWS_ACCESS_KEY_ID={MINIO_USER}\nAWS_SECRET_ACCESS_KEY={MINIO_PASSWORD}\n")
    manifest = {
        "version": 1,
        "host": "ci-host",
        "repository": {"url": repository, "password_file": str(password), "env_file": str(env)},
        "retention": {"daily": 7},
        "system": {
            "ansible": {
                "source": PLAYBOOK,
                "ref": "v1.1.0",
                "playbook": "playbooks/site.yml",
                # Safe on a CI runner: the runner's SSH access and firewall stay as they are.
                "skip_tags": ["ssh", "ufw"],
                "extra_vars": {
                    "hardening_target": "localhost",
                    "sysctl_hardening_ip_forward": True,  # Docker needs forwarding
                    "auditd_grub_config": False,  # the runner is never rebooted
                    "users_purge_sudoers_files": [],  # keep the runner's own sudo
                },
            },
            "paths": [
                {"path": "/etc/nginx", "restore": True},
                {"path": "/etc/letsencrypt"},
                {
                    "path": "/etc/systemd/system",
                    "include": ["drkit-demo-*.service"],
                    "restore": True,
                },
            ],
            "tls": {"strategy": "restore"},
        },
        "projects": [
            {
                "name": "demo",
                "path": str(PROJECT),
                "git": {"remote": str(REMOTE)},
                "compose": {"project_name": "demo", "files": ["compose.yaml"]},
                "secrets": [".env"],
                "databases": [{"engine": "postgres", "service": "db", "database": "shop"}],
                "volumes": [{"name": "uploads"}],
                "hooks": {
                    "post_restore": [
                        'echo "$DRKIT_PROJECT $COMPOSE_PROJECT_NAME" > restored-by-hook',
                        "docker compose ps --services > services.txt",
                    ]
                },
                # Through nginx: green only once nginx and the project are both back.
                "healthchecks": [{"url": "http://127.0.0.1/www/health", "timeout": 120}],
            }
        ],
    }
    (ETC / "dr.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    for path in ETC.iterdir():
        path.chmod(0o600)


def disaster() -> None:
    """The project, its data and the host configuration are gone; the storage is not."""
    subprocess.run(
        ["docker", "compose", "-p", "demo", "down", "--volumes", "--remove-orphans"],
        check=True,
        capture_output=True,
    )
    shutil.rmtree(PROJECT)
    SITE.unlink()
    SITE_LINK.unlink()
    shutil.rmtree("/etc/letsencrypt")
    UNIT.unlink()
    sh("systemctl", "daemon-reload")
    sh("systemctl", "stop", "nginx")


def test_restore_host_rebuilds_projects_and_nginx(storage: str) -> None:
    assert shutil.which("drkit") == "/usr/local/bin/drkit", "drkit comes from bootstrap.sh"
    deploy()
    configure(storage)
    assert drkit("init").returncode == 0
    doctor = drkit("doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    backup = drkit("backup")
    assert backup.returncode == 0, backup.stdout + backup.stderr
    before = fixtures()

    disaster()
    with pytest.raises(httpx.HTTPError):
        httpx.get("http://127.0.0.1/www/health", timeout=5)
    dry_run = drkit("restore", "host", "--dry-run")
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert "8. Recovery checklist and report" in dry_run.stdout

    result = drkit("--json", "restore", "host", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    _publish(report)
    assert [(s["number"], s["status"]) for s in report["steps"]] == [(n, "ok") for n in range(1, 9)]
    assert "ok=" in report["steps"][1]["detail"], "the playbook ran"
    [project] = report["steps"][4]["substeps"]
    assert [s["status"] for s in project["substeps"]] == ["ok"] * 10
    assert project["substeps"][0]["detail"] == "nothing to save: the project does not exist here"
    assert report["facts"]["rto_s"] > 0

    # Identical data and configuration, served again through nginx (HTTP and HTTPS).
    assert fixtures() == before
    assert httpx.get("http://127.0.0.1/www/health", timeout=10).text == "ok\n"
    assert httpx.get("https://127.0.0.1/www/health", timeout=10, verify=False).text == "ok\n"  # noqa: S501
    assert (PROJECT / ".env").stat().st_mode & 0o777 == 0o600
    assert (PROJECT / "restored-by-hook").read_text() == "demo demo\n"
    assert "web" in (PROJECT / "services.txt").read_text().split()
    notes = "\n".join(report["notes"])
    assert "DNS: point demo.test to this server" in notes
    assert "Units restored but not enabled by drkit: drkit-demo-report.service" in notes


def _publish(report: dict[str, Any]) -> None:
    """Per-step timings as Markdown, for the CI job summary."""
    target = os.environ.get("DRKIT_HOST_SUMMARY")
    if not target:
        return
    lines = [
        "## Host recovery",
        "",
        f"RTO (restore start → all healthchecks green): **{report['facts'].get('rto_s')}s**"
        f" · total {report['duration_s']}s · run `{report['run_id']}`",
        "",
        "| # | Step | Status | Time | Detail |",
        "|---|---|---|---:|---|",
    ]
    for step in report["steps"]:
        lines.append(
            f"| {step['number']} | {step['title']} | {step['status']} | "
            f"{step['duration_s']}s | {(step['detail'] or step['error'] or '').replace('|', '/')} |"
        )
        for project in step["substeps"]:
            for sub in project["substeps"]:
                lines.append(
                    f"| {step['number']}.{sub['number']} | {project['title']}: {sub['title']} | "
                    f"{sub['status']} | {sub['duration_s']}s | |"
                )
    Path(target).write_text("\n".join(lines) + "\n")
