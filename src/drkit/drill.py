"""``drkit drill --local`` (DESIGN §8): prove that a backup brings a server back.

On a disposable machine, the drill deploys the demo application (``examples/demo-app``:
PostgreSQL, MySQL and an HTTP app storing uploads in a volume, behind nginx with TLS), seeds
deterministic data, backs it up to a local S3 server, destroys the application, its data, its
images, its nginx site and certificate, and runs ``restore host --skip-system``. It passes
only if every row and every file comes back identical and the healthchecks are green, and
reports the RPO, the RTO and the time of every step.

It touches only its own resources (``drkit-demo*`` compose projects, ``/srv/drkit-demo``, its
nginx site and certificate), but it removes their images and needs root: run it on a
disposable host, as the CI drill does.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml

from drkit import __version__, docker, doctor, git, runner, state
from drkit.config import LoadedConfig, load_config
from drkit.errors import DrkitError, MissingDependencyError
from drkit.orchestrator import restic_for, run_backup
from drkit.restore import host as host_restore
from drkit.restore import steps as restore_steps
from drkit.restore.steps import NestedStepError, Outcome, RestorePlan, RestoreReport, Step

HOST = "drkit-drill"
PROJECT = "drkit-demo"
STORAGE_PROJECT = "drkit-demo-storage"
PROJECT_DIR = Path("/srv/drkit-demo")
DOMAIN = "drkit-demo.test"
APP_PORT = 18099
NGINX_HTTP = "http://127.0.0.1:18080"
NGINX_HTTPS = "https://127.0.0.1:18443"
S3_ENDPOINT = "http://127.0.0.1:19000"
S3_USER, S3_PASSWORD = "drkit-drill", "drkit-drill-only"  # matches storage/compose.yaml
SITES_AVAILABLE = Path("/etc/nginx/sites-available")
SITES_ENABLED = Path("/etc/nginx/sites-enabled")
LETSENCRYPT = Path("/etc/letsencrypt")
ORDERS = 2000
EVENTS = 2000
UPLOADS = 40
COMMAND_TIMEOUT_S = 600.0

NGINX = doctor.Dependency(
    name="nginx",
    version_command=("nginx", "-v"),
    version_pattern=re.compile(r"nginx/(\d+\.\d+\.\d+)"),
    minimum=None,
    install_hint="apt-get install nginx",
)
OPENSSL = doctor.Dependency(
    name="openssl",
    version_command=("openssl", "version"),
    version_pattern=re.compile(r"OpenSSL (\d+\.\d+\.\d+)"),
    minimum=None,
    install_hint="apt-get install openssl",
)

log = structlog.get_logger(__name__)


@dataclass
class Drill:
    """State of one drill: where things are, what was measured."""

    drill_id: str
    work: Path
    demo: Path
    secrets: dict[str, str] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    backup_finished: datetime | None = None
    disaster_at: datetime | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def config_path(self) -> Path:
        return self.work / "dr.yaml"

    def loaded(self) -> LoadedConfig:
        loaded = load_config(self.config_path)
        runner.redactor.add_all(loaded.secret_values())
        return loaded


def demo_app_dir() -> Path:
    """The demo application: packaged with drkit, or in the source checkout."""
    packaged = Path(str(resources.files("drkit") / "demo_app"))
    if (packaged / "compose.yaml").is_file():
        return packaged
    return Path(__file__).resolve().parents[2] / "examples" / "demo-app"


def plan(work: Path | None = None) -> tuple[RestorePlan, Drill]:
    drill_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)
    drill = Drill(
        drill_id=drill_id,
        work=work or state.state_dir() / "drill" / drill_id,
        demo=demo_app_dir(),
    )
    steps = [
        Step("Prepare the host", lambda: prepare(drill)),
        Step("Start the storage (MinIO)", lambda: start_storage(drill)),
        Step("Deploy the demo application", lambda: deploy(drill)),
        Step("Seed data and record fixtures", lambda: seed(drill)),
        Step("Back up", lambda: backup(drill)),
        Step("Disaster", lambda: disaster(drill)),
        Step("Restore the host (restore host --skip-system)", lambda: restore(drill)),
        Step("Validate the data", lambda: validate(drill)),
    ]
    drill_plan = RestorePlan(
        kind="drill",
        target=HOST,
        run_id="",
        steps=steps,
        summary="Deploy, back up, destroy and restore the demo application on this host.",
        restore_id=drill_id,
    )
    drill.facts = drill_plan.facts
    return drill_plan, drill


def run(*, on_step: Callable[[restore_steps.StepResult], None] | None = None) -> RestoreReport:
    """Run a drill and record it (reports directory, ``state.json``, CI summary)."""
    drill_plan, drill = plan()
    report = restore_steps.execute(drill_plan, on_step=on_step)
    report.run_id = drill.facts.get("run_id", "")
    try:
        restore_steps.save_report(report)
    except DrkitError as exc:
        report.errors.append(f"report: {exc.message}")
    try:
        state.record_drill(
            state.DrillRecord(
                drill_id=drill.drill_id,
                status="ok" if report.ok else "failed",
                finished_at=report.finished_at or datetime.now(UTC),
                rto_s=drill.facts.get("rto_s"),
                rpo_s=drill.facts.get("rpo_s"),
            )
        )
    except DrkitError as exc:
        report.errors.append(f"state: {exc.message}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(to_markdown(report))
    return report


# --------------------------------------------------------------------------- steps


def prepare(drill: Drill) -> str:
    """Step 1: a root shell with every tool the drill needs, and no previous drill around."""
    if os.geteuid() != 0:
        raise DrkitError(
            "the drill must run as root", hint="run it with sudo, on a disposable host"
        )
    checks = [
        doctor.check_dependency(dependency)
        for dependency in (*doctor.REQUIRED_DEPENDENCIES, NGINX, OPENSSL)
    ]
    failed = [check for check in checks if check.status is not doctor.Status.OK]
    if failed:
        raise MissingDependencyError(
            "; ".join(f"{check.name}: {check.detail}" for check in failed),
            hint="run scripts/bootstrap.sh and install nginx",
        )
    _cleanup()
    drill.work.mkdir(mode=0o700, parents=True)
    drill.facts["environment"] = environment(checks)
    return ", ".join(f"{check.name} {check.detail}" for check in checks)


def start_storage(drill: Drill) -> str:
    """Step 2: the off-site storage. It survives the disaster, as a real bucket would."""
    _compose(STORAGE_PROJECT, drill.demo / "storage", "up", "--detach", "--wait")
    return f"MinIO on {S3_ENDPOINT}"


def deploy(drill: Drill) -> str:
    """Step 3: the application as deployed from git, behind nginx with TLS."""
    repo = drill.work / "demo.git"
    source = drill.work / "source"
    shutil.copytree(drill.demo, source, ignore=shutil.ignore_patterns("storage", "__pycache__"))
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        [
            *("-c", "user.name=drkit drill", "-c", "user.email=drill@drkit.invalid"),
            *("-c", "commit.gpgsign=false", "commit", "-q", "-m", "demo application"),
        ],
        ["clone", "-q", "--bare", ".", str(repo)],
    ):
        runner.run(git.args(source, *args), timeout=COMMAND_TIMEOUT_S)
    git.clone(str(repo), PROJECT_DIR, None)
    git.checkout_ref(PROJECT_DIR, "main")

    drill.secrets = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(18),
        "MYSQL_ROOT_PASSWORD": secrets.token_urlsafe(18),
        "MYSQL_PASSWORD": secrets.token_urlsafe(18),
    }
    runner.redactor.add_all(drill.secrets.values())
    env = PROJECT_DIR / ".env"
    env.write_text(
        "".join(f"{key}={value}\n" for key, value in drill.secrets.items())
        + f"APP_PORT={APP_PORT}\n"
    )
    env.chmod(0o600)
    _compose(PROJECT, PROJECT_DIR, "up", "--detach", "--wait", "--wait-timeout", "300")

    live = LETSENCRYPT / "live" / DOMAIN
    live.mkdir(parents=True)
    runner.run(
        [
            *("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30"),
            *("-subj", f"/CN={DOMAIN}", "-keyout", str(live / "privkey.pem")),
            *("-out", str(live / "fullchain.pem")),
        ],
        timeout=COMMAND_TIMEOUT_S,
    )
    site = (drill.demo / "nginx" / "drkit-demo.conf").read_text()
    (SITES_AVAILABLE / PROJECT).write_text(
        site.replace("{domain}", DOMAIN).replace("{app_port}", str(APP_PORT))
    )
    (SITES_ENABLED / PROJECT).symlink_to(SITES_AVAILABLE / PROJECT)
    runner.run(["nginx", "-t"], timeout=COMMAND_TIMEOUT_S)
    runner.run(["systemctl", "reload-or-restart", "nginx"], timeout=COMMAND_TIMEOUT_S)
    _get(f"{NGINX_HTTP}/health")
    return f"{PROJECT} running in {PROJECT_DIR}, served by nginx on {NGINX_HTTP} and {NGINX_HTTPS}"


def seed(drill: Drill) -> str:
    """Step 4: deterministic data in both databases and the uploads volume, and its fixtures."""
    _psql(
        "CREATE TABLE orders (id int PRIMARY KEY, customer text, amount numeric(10, 2), "  # noqa: S608 - constants only
        "created date); INSERT INTO orders SELECT i, 'customer-' || (i % 97), "
        f"round((i * 7919 % 100000) / 100.0, 2), date '2026-01-01' + (i % 365) "
        f"FROM generate_series(1, {ORDERS}) AS i;"
    )
    _mysql(
        drill,
        "CREATE TABLE events (id int PRIMARY KEY, kind varchar(32), payload char(64)); "  # noqa: S608
        f"SET SESSION cte_max_recursion_depth = {EVENTS + 1}; "
        "INSERT INTO events (id, kind, payload) WITH RECURSIVE seq (n) AS "
        f"(SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < {EVENTS}) "
        "SELECT n, CONCAT('kind-', n % 13), SHA2(CONCAT('event-', n), 256) FROM seq;",
    )
    with httpx.Client(timeout=30) as client:
        for index in range(UPLOADS):
            response = client.put(
                f"{NGINX_HTTP}/uploads/{upload_name(index)}", content=upload(index)
            )
            response.raise_for_status()
    drill.expected = fixtures(drill)
    return (
        f"{ORDERS} orders (PostgreSQL), {EVENTS} events (MySQL), {UPLOADS} uploaded files "
        f"({sum(len(upload(i)) for i in range(UPLOADS)) // 1024} KiB)"
    )


def backup(drill: Drill) -> str:
    """Step 5: a full run of drkit backup, configured as an operator would."""
    write_config(drill)
    loaded = drill.loaded()
    restic_for(loaded.manifest, dict(loaded.env)).init()
    report = run_backup(loaded)
    if report.manifest.status != "ok":
        failed = "; ".join(f"{i.label}: {i.error}" for i in report.manifest.failed_items)
        raise DrkitError(
            f"backup run {report.manifest.run_id} is {report.manifest.status}: {failed}"
        )
    drill.backup_finished = report.manifest.finished_at
    drill.facts["run_id"] = report.manifest.run_id
    drill.facts["backup_s"] = round(
        (report.manifest.finished_at - report.manifest.started_at).total_seconds(), 1
    )
    size = sum(item.bytes for item in report.manifest.items)
    return f"run {report.manifest.run_id}: {len(report.manifest.items)} items, {size // 1024} KiB"


def disaster(drill: Drill) -> str:
    """Step 6: the application, its data, its images, its site and certificate are gone."""
    # --rmi all: the restore pulls the images again, as on a new server.
    _compose(PROJECT, PROJECT_DIR, "down", "--volumes", "--remove-orphans", "--rmi", "all")
    shutil.rmtree(PROJECT_DIR)
    (SITES_ENABLED / PROJECT).unlink()
    (SITES_AVAILABLE / PROJECT).unlink()
    shutil.rmtree(LETSENCRYPT / "live" / DOMAIN)
    runner.run(["systemctl", "reload", "nginx"], timeout=COMMAND_TIMEOUT_S)
    drill.disaster_at = datetime.now(UTC)
    if drill.backup_finished is not None:
        drill.facts["rpo_s"] = round((drill.disaster_at - drill.backup_finished).total_seconds(), 1)
    return "containers, volumes, images, project directory, nginx site and certificate removed"


def restore(drill: Drill) -> Outcome:
    """Step 7: drkit restore host, without Ansible (the host itself is still provisioned)."""
    loaded = drill.loaded()
    restic = restic_for(loaded.manifest, dict(loaded.env))
    host_plan = host_restore.plan(loaded, restic, skip_system=True)
    report = restore_steps.run(host_plan)
    if not report.ok:
        failed = report.failed_step
        raise NestedStepError(
            f"restore host failed at step {failed.number if failed else '?'}: "
            f"{failed.error if failed else 'unknown error'}",
            substeps=report.steps,
        )
    drill.facts["rto_s"] = report.facts.get("rto_s")
    drill.facts["restore_s"] = report.duration_s
    drill.facts["restore_report"] = str(report.report_path) if report.report_path else None
    return Outcome(f"RTO {drill.facts['rto_s']}s", report.steps)


def validate(drill: Drill) -> str:
    """Step 8: every row and every file is back, and nginx serves the application over TLS."""
    actual = fixtures(drill)
    drill.facts["fixtures"] = {"expected": drill.expected, "actual": actual}
    different = sorted(key for key in drill.expected if drill.expected[key] != actual.get(key))
    drill.facts["integrity"] = "identical" if not different else "different"
    if different:
        raise DrkitError(f"restored data differs from the backed-up data: {', '.join(different)}")
    _get(f"{NGINX_HTTPS}/health", verify=False)
    return (
        f"identical: {actual['orders']['rows']} orders, {actual['events']['rows']} events, "
        f"{len(actual['uploads'])} files; HTTPS through nginx answers"
    )


# --------------------------------------------------------------------------- data


def upload_name(index: int) -> str:
    return f"file-{index:03d}.bin"


def upload(index: int) -> bytes:
    """Deterministic content, 1 to 64 KiB, different for every file."""
    size = 1024 * (1 + (index * 37) % 64)
    block = hashlib.sha256(f"upload-{index}".encode()).digest()
    return (block * (size // len(block) + 1))[:size]


def fixtures(drill: Drill) -> dict[str, Any]:
    """What must come back identical: row counts and checksums, and every file's SHA-256."""
    orders = _psql(
        "SELECT count(*), md5(string_agg(id || '|' || customer || '|' || amount || '|' || "
        "created, ',' ORDER BY id)) FROM orders"
    ).split("|")
    events = _mysql(
        drill,
        "SELECT COUNT(*), SUM(CRC32(CONCAT_WS('|', id, kind, payload))) FROM events",
    ).split()
    with httpx.Client(timeout=30) as client:
        names = client.get(f"{NGINX_HTTP}/uploads/").raise_for_status().text.split()
        files = {
            name: hashlib.sha256(client.get(f"{NGINX_HTTP}/uploads/{name}").content).hexdigest()
            for name in names
        }
    return {
        "orders": {"rows": int(orders[0]), "checksum": orders[1]},
        "events": {"rows": int(events[0]), "checksum": events[1]},
        "uploads": files,
    }


def write_config(drill: Drill) -> None:
    """dr.yaml, restic.pass and env of the drill, owner-only like an operator's."""
    password = drill.work / "restic.pass"
    password.write_text(secrets.token_urlsafe(32) + "\n")
    env = drill.work / "env"
    env.write_text(f"AWS_ACCESS_KEY_ID={S3_USER}\nAWS_SECRET_ACCESS_KEY={S3_PASSWORD}\n")
    manifest = {
        "version": 1,
        "host": HOST,
        "repository": {
            "url": f"s3:{S3_ENDPOINT}/drkit-drill/{drill.drill_id}",
            "password_file": str(password),
            "env_file": str(env),
        },
        "retention": {"daily": 7},
        "system": {
            "paths": [
                {"path": str(SITES_AVAILABLE), "include": [PROJECT], "restore": True},
                {"path": str(SITES_ENABLED), "include": [PROJECT], "restore": True},
                {"path": str(LETSENCRYPT), "include": [f"live/{DOMAIN}/*"]},
            ],
            "tls": {"strategy": "restore"},
        },
        "projects": [
            {
                "name": PROJECT,
                "path": str(PROJECT_DIR),
                "git": {"remote": str(drill.work / "demo.git")},
                "compose": {"project_name": PROJECT, "files": ["compose.yaml"]},
                "secrets": [".env"],
                "databases": [
                    {"engine": "postgres", "service": "db", "database": "shop"},
                    {"engine": "mysql", "service": "mysql", "database": "analytics"},
                ],
                "volumes": [{"name": "uploads"}],
                # Through nginx: green only once nginx and the application are both back.
                "healthchecks": [{"url": f"{NGINX_HTTP}/health", "timeout": 180}],
            }
        ],
    }
    drill.config_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    for path in (password, env, drill.config_path):
        path.chmod(0o600)


def environment(checks: list[doctor.Check]) -> dict[str, Any]:
    """Where the drill ran: the numbers mean little without it."""
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    pretty = next(
        (line.split("=", 1)[1].strip('"') for line in os_release.splitlines()
         if line.startswith("PRETTY_NAME=")),
        platform.platform(),
    )  # fmt: skip
    memory_kib = next(
        (int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
         if line.startswith("MemTotal:")),
        0,
    )  # fmt: skip
    return {
        "drkit": __version__,
        "os": pretty,
        "kernel": platform.release(),
        "cpus": os.cpu_count(),
        "memory_gib": round(memory_kib / 1024 / 1024, 1),
        "tools": {check.name: check.detail for check in checks},
        "runner": _github_runner(),
    }


def to_markdown(report: RestoreReport) -> str:
    """The drill result for a CI job summary."""
    facts = report.facts
    status = "passed" if report.ok else "FAILED"
    env = facts.get("environment") or {}
    lines = [
        f"## DR drill {status}",
        "",
        "| | |",
        "|---|---|",
        f"| RTO (restore start → all healthchecks green) | **{_seconds(facts.get('rto_s'))}** |",
        f"| RPO (backup end → disaster) | **{_seconds(facts.get('rpo_s'))}** |",
        f"| Data integrity | {facts.get('integrity', 'not checked')} |",
        f"| Backup run | `{facts.get('run_id', '?')}` ({_seconds(facts.get('backup_s'))}) |",
        f"| Environment | {env.get('runner') or env.get('os', '?')}, {env.get('cpus', '?')} CPUs, "
        f"{env.get('memory_gib', '?')} GiB · drkit {env.get('drkit', '?')} |",
        "",
        "| # | Step | Status | Time |",
        "|---|---|---|---:|",
    ]
    lines += _step_rows(report.steps)
    failed = report.failed_step
    if failed is not None:
        lines += ["", f"**Failed at step {failed.number}:** {failed.error}"]
    return "\n".join(lines) + "\n\n"


def _step_rows(results: list[restore_steps.StepResult], prefix: str = "") -> list[str]:
    """Table rows for steps and their nested steps, numbered ``7``, ``7.5``, ``7.5.4``."""
    rows = []
    for step in results:
        number = f"{prefix}{step.number}"
        rows.append(f"| {number} | {step.title} | {step.status} | {step.duration_s}s |")
        rows += _step_rows(step.substeps, f"{number}.")
    return rows


# --------------------------------------------------------------------------- helpers


def _compose(project: str, directory: Path, *args: str) -> runner.CommandResult:
    return docker.compose_run(
        project, [directory / "compose.yaml"], directory, args, timeout=COMMAND_TIMEOUT_S
    )


def _psql(statement: str) -> str:
    return _compose(
        PROJECT, PROJECT_DIR, "exec", "-T", "db",
        "psql", "-U", "shop", "-d", "shop", "-v", "ON_ERROR_STOP=1", "-XtAc", statement,
    ).stdout.strip()  # fmt: skip


def _mysql(drill: Drill, statement: str) -> str:
    # The password travels through the environment, never on the command line.
    result = runner.run(
        [
            *docker.compose_args(PROJECT, [PROJECT_DIR / "compose.yaml"], PROJECT_DIR),
            *("exec", "-T", "-e", "MYSQL_PWD", "mysql"),
            *("mysql", "-u", "analytics", "analytics", "--batch", "--skip-column-names"),
            *("-e", statement),
        ],
        timeout=COMMAND_TIMEOUT_S,
        env={"MYSQL_PWD": drill.secrets["MYSQL_PASSWORD"]},
    )
    return result.stdout.strip()


def _get(url: str, *, verify: bool = True, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            httpx.get(url, timeout=5, verify=verify).raise_for_status()
            return
        except httpx.HTTPError as exc:
            if time.monotonic() > deadline:
                raise DrkitError(f"{url} does not answer: {exc}") from None
            time.sleep(1)


def _cleanup() -> None:
    """Remove what a previous drill left behind (the drill can be run again)."""
    for project in (PROJECT, STORAGE_PROJECT):
        runner.run(
            ["docker", "compose", "-p", project, "down", "--volumes", "--remove-orphans"],
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    for path in [PROJECT_DIR, *PROJECT_DIR.parent.glob(f"{PROJECT_DIR.name}.pre-restore-*")]:
        shutil.rmtree(path, ignore_errors=True)
    for path in (SITES_ENABLED / PROJECT, SITES_AVAILABLE / PROJECT):
        path.unlink(missing_ok=True)
    shutil.rmtree(LETSENCRYPT / "live" / DOMAIN, ignore_errors=True)


def _github_runner() -> str | None:
    """The GitHub runner image, from the variables runners set (``ImageOS``, ``ImageVersion``)."""
    image = os.environ.get("ImageOS")  # noqa: SIM112 - the runner's own spelling
    version = os.environ.get("ImageVersion", "")  # noqa: SIM112
    return f"GitHub Actions {image} {version}".strip() if image else None


def _seconds(value: Any) -> str:
    return f"{value:g}s" if isinstance(value, int | float) else "n/a"
