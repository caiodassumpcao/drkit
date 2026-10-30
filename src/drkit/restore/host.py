"""``drkit restore host`` (DESIGN §7.3): rebuild a lost server on a fresh Ubuntu machine.

The host is rebuilt, not cloned (ADR-0002): Ansible provisions the operating system, then the
configuration that cannot be rebuilt (nginx sites, units, certificates) and every project
come back from one run. The report times each step and measures the RTO.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from drkit import doctor, git, health, runner, state
from drkit.config import Ansible, LoadedConfig, Manifest, SystemPath
from drkit.errors import CommandError, DrkitError, MissingDependencyError
from drkit.restic import Restic
from drkit.restore import project as project_restore
from drkit.restore import steps as restore_steps
from drkit.restore import tls
from drkit.restore.steps import NestedStepError, Outcome, RestorePlan, Step, StepResult
from drkit.runmanifest import RunManifest, find_run, new_run_id
from drkit.sources.base import ItemResult
from drkit.sources.system import INVENTORY_FILENAME, collect_inventory, parse_os_release

SUPPORTED_UBUNTU = ("22.04", "24.04")
OS_RELEASE = Path("/etc/os-release")
SYSTEMD_DIR = Path("/etc/systemd/system")
REBOOT_REQUIRED = Path("/run/reboot-required")
ANSIBLE_TIMEOUT_S = 3600.0
GALAXY_TIMEOUT_S = 900.0
GALAXY_RETRY_DELAYS_S = (10.0, 30.0)
COMMAND_TIMEOUT_S = 120.0
# Package lists in notes are cut here; the report keeps them whole.
MAX_LISTED = 15

log = structlog.get_logger(__name__)
sleep = time.sleep  # replaced in tests


@dataclass
class HostRestore:
    """State shared by the steps of one host restore."""

    loaded: LoadedConfig
    restic: Restic
    run: RunManifest
    restore_id: str
    projects: list[RestorePlan]
    skip_system: bool
    paths: ItemResult | None
    inventory: ItemResult | None
    nginx_dir: Path = field(default_factory=lambda: tls.NGINX_DIR)
    letsencrypt: Path = field(default_factory=lambda: tls.LETSENCRYPT)
    started: float | None = None
    healthy_at: float | None = None
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest(self) -> Manifest:
        return self.loaded.manifest

    def timed(self, step: Step) -> Step:
        """Start the RTO clock with the first step that runs (also when resuming)."""

        def action() -> str | Outcome | None:
            if self.started is None:
                self.started = time.monotonic()
            return step.action()

        return Step(step.title, action, step.plan)


def plan(
    loaded: LoadedConfig,
    restic: Restic,
    *,
    run_id: str = "latest",
    skip_system: bool = False,
) -> RestorePlan:
    """Plan the eight steps of ``drkit restore host`` (DESIGN §7.3)."""
    manifest = loaded.manifest

    def complete(run: RunManifest) -> bool:
        has_paths = not manifest.system.paths or _item(run, "paths") is not None
        return has_paths and all(
            project_restore.holds_project(run, p.name) for p in manifest.projects
        )

    run = find_run(restic, run_id, accept=complete, what="every project of the manifest")
    paths_item = _item(run, "paths")
    if manifest.system.paths and (paths_item is None or paths_item.status != "ok"):
        raise DrkitError(
            f"run {run.run_id} has no backup of system.paths",
            hint="pick another run with --run (see `drkit runs list`)",
        )
    tls_config = manifest.system.tls
    if tls_config is not None and tls_config.strategy == "restore":
        _covering(manifest.system.paths, tls.LETSENCRYPT)  # fails early if not backed up
    restore_id = new_run_id()
    projects = [
        project_restore.plan_for_run(
            loaded, project, restic, run, healthchecks_deferred=True, restore_id=restore_id
        )
        for project in manifest.projects
    ]
    host = HostRestore(
        loaded=loaded,
        restic=restic,
        run=run,
        restore_id=restore_id,
        projects=projects,
        skip_system=skip_system,
        paths=paths_item,
        inventory=_item(run, "inventory"),
    )
    ansible = manifest.system.ansible
    restorable = [entry for entry in manifest.system.paths if entry.restore]
    steps = [
        Step(
            "Preflight",
            lambda: preflight(host),
            ("Ubuntu release, root, restic, Docker, Compose, git, Ansible, repository",),
        ),
        Step(
            "Provision the host with Ansible",
            lambda: provision(host),
            _ansible_plan(ansible, skip_system),
        ),
        Step(
            "Restore system paths",
            lambda: restore_paths(host),
            tuple(f"{entry.path}" for entry in restorable) or ("none marked restore: true",),
        ),
        Step(
            "Reload systemd and validate nginx",
            lambda: reload_system(host),
            ("systemctl daemon-reload", "nginx -t (when nginx is configured)"),
        ),
        Step(
            "Restore the projects",
            lambda: restore_projects(host),
            tuple(
                f"{p.target}: {len(p.steps)} steps (healthchecks run at step 7)" for p in projects
            )
            or ("no project",),
        ),
        Step("TLS certificates", lambda: restore_tls(host), _tls_plan(host)),
        Step(
            "Reload nginx and run all healthchecks",
            lambda: start_and_check(host),
            (
                "nginx -t, then start or reload nginx",
                *(
                    f"GET {check.url} → {check.expect_status}"
                    for project in manifest.projects
                    for check in project.healthchecks
                ),
            ),
        ),
        Step(
            "Recovery checklist and report",
            lambda: checklist(host),
            ("DNS and post-recovery checklist, package inventory diff, RTO",),
        ),
    ]
    restore_plan = RestorePlan(
        kind="host",
        target=manifest.host,
        run_id=run.run_id,
        steps=[host.timed(step) for step in steps],
        summary=(
            f"Rebuild host {manifest.host} from run {run.run_id}: provision it, restore "
            f"{len(restorable)} system path(s) and {len(projects)} project(s) "
            "(their databases and volumes are replaced)."
        ),
        warnings=[warning for p in projects for warning in _prefixed(p)],
        restore_id=restore_id,
    )
    # The steps write their notes and measurements straight into the plan.
    host.notes = restore_plan.notes
    host.facts = restore_plan.facts
    return restore_plan


# --------------------------------------------------------------------------- steps


def preflight(host: HostRestore) -> str:
    """Step 1: the machine can run the restore (checked before anything changes)."""
    if os.geteuid() != 0:
        raise DrkitError(
            "restore host must run as root: it writes /etc, volumes and system units",
            hint="run it with sudo",
        )
    os_release = parse_os_release(_read(OS_RELEASE)) or {}
    release = f"{os_release.get('NAME', '?')} {os_release.get('VERSION_ID', '?')}"
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") not in SUPPORTED_UBUNTU:
        host.notes.append(
            f"{release} is not a supported release (Ubuntu {' / '.join(SUPPORTED_UBUNTU)}): "
            "check the result with extra care"
        )
    required = list(doctor.REQUIRED_DEPENDENCIES)
    if host.manifest.system.ansible is not None and not host.skip_system:
        required.append(doctor.ANSIBLE)
    checks = [doctor.check_dependency(dependency) for dependency in required]
    failed = [check for check in checks if check.status is not doctor.Status.OK]
    if failed:
        raise MissingDependencyError(
            "; ".join(f"{check.name}: {check.detail}" for check in failed),
            hint="run scripts/bootstrap.sh, which installs everything restore host needs",
        )
    host.restic.ensure_ready()
    versions = ", ".join(f"{check.name} {check.detail}" for check in checks)
    return f"{release}; {versions}; run {host.run.run_id}"


def provision(host: HostRestore) -> str:
    """Step 2: rebuild the operating system with the Ansible playbook (``-c local``)."""
    ansible = host.manifest.system.ansible
    if host.skip_system:
        return "skipped (--skip-system)"
    if ansible is None:
        return "no playbook configured (system.ansible)"
    reports = state.state_dir() / restore_steps.REPORTS_DIR
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = reports / f"ansible-{host.restore_id}.log"
    with tempfile.TemporaryDirectory(prefix="drkit-ansible-") as work:
        return run_playbook(ansible, Path(work), log_path)


def run_playbook(ansible: Ansible, work: Path, log_path: Path) -> str:
    """Fetch the playbook at its ref, install its requirements and run it on this host."""
    source = Path(ansible.source)
    if source.is_dir() and not (source / ".git").exists():
        checkout = source  # a plain local directory: used as it is
    else:
        checkout = work / "playbook"
        git.clone(ansible.source, checkout, None)
        git.checkout_ref(checkout, ansible.ref or "HEAD")
    playbook = checkout / ansible.playbook
    if not playbook.is_file():
        raise DrkitError(
            f"playbook {ansible.playbook} not found in {ansible.source}",
            hint="set system.ansible.playbook to its path inside the repository",
        )
    if (checkout / "requirements.yml").is_file():
        _install_requirements(checkout)

    extra_vars = work / "extra-vars.json"
    extra_vars.touch(mode=0o600)
    extra_vars.write_text(json.dumps(ansible.extra_vars), encoding="utf-8")
    args = ["ansible-playbook", "-i", "localhost,", "-c", "local", "-e", f"@{extra_vars}"]
    if ansible.skip_tags:
        args += ["--skip-tags", ",".join(ansible.skip_tags)]
    log.info("host.ansible", source=ansible.source, ref=ansible.ref, log=str(log_path))
    result = runner.run(
        [*args, str(playbook)],
        timeout=ANSIBLE_TIMEOUT_S,
        cwd=checkout,  # so the repository's ansible.cfg applies
        plain_env={"ANSIBLE_LOG_PATH": str(log_path), "ANSIBLE_NOCOLOR": "1"},
        check=False,
    )
    if not result.ok:
        raise CommandError(
            f"ansible-playbook failed with exit code {result.returncode}: "
            f"{runner.failure_detail(result, max_lines=4)}",
            hint=f"full log: {log_path}; fix the playbook or its variables and resume with "
            "--from-step 2",
        )
    recap = re.search(r"^localhost\s*:\s*(ok=.*?)\s*$", result.stdout, re.MULTILINE)
    where = f"{ansible.source}@{ansible.ref}" if ansible.ref else ansible.source
    skipped = f", skipped tags {', '.join(ansible.skip_tags)}" if ansible.skip_tags else ""
    summary = " ".join(recap.group(1).split()[:3]) if recap else "done"
    return f"{ansible.playbook} from {where}: {summary}{skipped} (log: {log_path})"


def restore_paths(host: HostRestore) -> str:
    """Step 3: put back the configuration marked ``restore: true`` (nginx sites, units…)."""
    entries = [entry for entry in host.manifest.system.paths if entry.restore]
    if not entries:
        return "none marked restore: true"
    snapshot_id = _paths_snapshot(host)
    for entry in entries:
        _restore_path(host.restic, snapshot_id, entry.path)
    return f"{len(entries)} path(s) restored: {', '.join(str(e.path) for e in entries)}"


def reload_system(host: HostRestore) -> str:
    """Step 4: systemd sees the restored units; the restored nginx configuration is valid."""
    details = []
    if shutil.which("systemctl"):
        runner.run(["systemctl", "daemon-reload"], timeout=COMMAND_TIMEOUT_S)
        details.append("systemd reloaded")
    if not (host.nginx_dir / "nginx.conf").is_file():
        return "; ".join([*details, "no nginx configuration"])
    result = _nginx_test()
    tls_config = host.manifest.system.tls
    if result.ok:
        details.append("nginx configuration valid")
    elif tls_config is not None and tls.missing_certificates_only(result.stderr):
        how = "issued" if tls_config.strategy == "reissue" else "restored"
        details.append(f"nginx configuration waits for its certificates ({how} in step 6)")
    else:
        raise CommandError(
            f"nginx -t failed: {runner.failure_detail(result, max_lines=3)}",
            hint="fix the restored configuration in /etc/nginx and resume with --from-step 4",
        )
    return "; ".join(details)


def restore_projects(host: HostRestore) -> Outcome:
    """Step 5: every project, in manifest order, each with its own timed steps."""
    results: list[StepResult] = []
    for project_plan in host.projects:
        started = time.monotonic()
        report = restore_steps.execute(project_plan)
        result = StepResult(
            number=len(results) + 1,
            title=f"Project {project_plan.target}",
            status="ok" if report.ok else "failed",
            duration_s=round(time.monotonic() - started, 1),
            substeps=report.steps,
        )
        results.append(result)
        failed = report.failed_step
        if failed is not None:
            result.error = failed.error
            raise NestedStepError(
                f"project {project_plan.target} stopped at its step {failed.number} "
                f"({failed.title}): {failed.error}",
                substeps=results,
                hint=f"fix the cause; `drkit restore project {project_plan.target} --run "
                f"{host.run.run_id} --from-step {failed.number}` resumes that project, then "
                "resume the host restore with --from-step 6",
            )
        result.detail = f"{len(report.steps)} steps"
    detail = ", ".join(f"{r.title.removeprefix('Project ')} ({r.duration_s:g}s)" for r in results)
    return Outcome(detail or "no project", results)


def restore_tls(host: HostRestore) -> str:
    """Step 6: certificates from the backup, or issued again by certbot."""
    tls_config = host.manifest.system.tls
    if tls_config is None:
        return "not configured (system.tls)"
    if tls_config.strategy == "restore":
        _restore_path(host.restic, _paths_snapshot(host), host.letsencrypt)
        names = sorted(p.name for p in (host.letsencrypt / "live").glob("*") if p.is_dir())
        return f"{len(names)} certificate(s) restored: {', '.join(names) or 'none'}"
    assert tls_config.email is not None  # noqa: S101 - required by the manifest for reissue
    check = doctor.check_dependency(doctor.CERTBOT)
    if check.status is not doctor.Status.OK:
        raise MissingDependencyError(f"certbot: {check.detail}", hint=doctor.CERTBOT.install_hint)
    return tls.reissue(tls_config.email, host.nginx_dir, host.letsencrypt)


def start_and_check(host: HostRestore) -> str:
    """Step 7: nginx serves again, and every project answers its healthchecks."""
    details = []
    if (host.nginx_dir / "nginx.conf").is_file():
        result = _nginx_test()
        if not result.ok:
            raise CommandError(
                f"nginx -t failed: {runner.failure_detail(result, max_lines=3)}",
                hint="fix /etc/nginx and resume with --from-step 7",
            )
        runner.run(["systemctl", "enable", "--now", "nginx"], timeout=COMMAND_TIMEOUT_S)
        runner.run(["systemctl", "reload", "nginx"], timeout=COMMAND_TIMEOUT_S)
        details.append("nginx running")
    checks = [check for project in host.manifest.projects for check in project.healthchecks]
    results = health.wait_all(checks)
    failed = [result for result in results if not result.ok]
    if failed:
        raise DrkitError(
            "; ".join(f"{result.url}: {result.error}" for result in failed),
            hint="check the service logs (`docker compose logs`) and nginx "
            "(`journalctl -u nginx`); resume with --from-step 7",
        )
    host.healthy_at = time.monotonic()
    details.append(f"{len(results)} healthcheck(s) green" if results else "no healthcheck")
    return "; ".join(details)


def checklist(host: HostRestore) -> str:
    """Step 8: what the operator still has to do, what changed, and how long it took."""
    if host.started is not None and host.healthy_at is not None:
        host.facts["rto_s"] = round(host.healthy_at - host.started, 1)
    domains = tls.all_domains(host.nginx_dir) if host.nginx_dir.is_dir() else []
    addresses = _addresses()
    host.notes.append(
        f"DNS: point {', '.join(domains) if domains else 'your domains'} to this server"
        + (f" ({', '.join(addresses)})" if addresses else "")
        + "; lower the TTL first if the old records are still cached"
    )
    host.notes.append(
        f"Backups: enable the drkit timers here (`systemctl enable --now drkit-backup.timer "
        f"drkit-verify.timer drkit-prune.timer`), and make sure the old server, if it comes "
        f"back, no longer backs up as {host.manifest.host}"
    )
    units = _restored_units(host.manifest.system.paths)
    if units:
        host.notes.append(
            "Units restored but not enabled by drkit: "
            + ", ".join(units)
            + " (`systemctl enable --now <unit>` for those that should run)"
        )
    tls_config = host.manifest.system.tls
    if tls_config is not None:
        host.notes.append("TLS: check that renewal works (`certbot renew --dry-run`)")
    if REBOOT_REQUIRED.exists():
        host.notes.append("A reboot is pending (/run/reboot-required): plan it")
    host.notes.append(
        f"Safety snapshots of this restore (tag restore:{host.restore_id}), if any, are kept "
        "until you forget them (see the runbook)"
    )
    diff = _inventory_diff(host)
    host.facts["inventory"] = diff
    summary = []
    if "rto_s" in host.facts:
        summary.append(f"RTO {host.facts['rto_s']:g}s")
    if diff:
        summary.append(
            f"packages: {len(diff['missing'])} missing, {len(diff['added'])} new since the backup"
        )
    summary.append(f"{len(host.notes)} checklist item(s)")
    return "; ".join(summary)


# --------------------------------------------------------------------------- helpers


def _item(run: RunManifest, name: str) -> ItemResult | None:
    return next((i for i in run.items if i.kind == "system" and i.name == name), None)


def _prefixed(project_plan: RestorePlan) -> list[str]:
    return [f"{project_plan.target}: {warning}" for warning in project_plan.warnings]


def _covering(paths: list[SystemPath], target: Path) -> SystemPath:
    for entry in paths:
        if target == entry.path or target.is_relative_to(entry.path):
            return entry
    raise DrkitError(
        f"tls.strategy is 'restore' but {target} is not in system.paths",
        hint=f"add {target} to system.paths so that it is backed up, or use tls.strategy: reissue",
    )


def _paths_snapshot(host: HostRestore) -> str:
    if host.paths is None or host.paths.snapshot is None:
        raise DrkitError(
            f"run {host.run.run_id} has no backup of system.paths",
            hint="pick another run with --run (see `drkit runs list`)",
        )
    return host.paths.snapshot


def _restore_path(restic: Restic, snapshot_id: str, path: Path) -> None:
    """Restore ``path`` (a directory, or files under it) to where it was. Files the snapshot
    does not have are left alone: Ansible may have created them."""
    snapshot = restic.snapshot(snapshot_id)
    if not any(Path(p) == path or Path(p).is_relative_to(path) for p in snapshot.paths):
        raise DrkitError(
            f"{path} is not in snapshot {snapshot_id[:8]} of system.paths",
            hint="it was added to system.paths after this backup: restore an older layout by "
            "hand, or take a new backup first",
        )
    path.mkdir(parents=True, exist_ok=True)
    restic.restore(snapshot_id, str(path), str(path))


def _install_requirements(checkout: Path) -> None:
    """``ansible-galaxy install -r requirements.yml`` (roles and collections), retried: Galaxy
    has outages, and a recovery should not stop because of one."""
    command = ["ansible-galaxy", "install", "-r", "requirements.yml"]
    for delay in (*GALAXY_RETRY_DELAYS_S, None):
        result = runner.run(command, timeout=GALAXY_TIMEOUT_S, cwd=checkout, check=False)
        if result.ok:
            return
        if delay is None:
            raise CommandError(
                "ansible-galaxy could not install requirements.yml: "
                f"{runner.failure_detail(result, max_lines=2)}",
                hint="check network access to galaxy.ansible.com and resume with --from-step 2",
            )
        log.warning("host.galaxy_retry", delay_s=delay)
        sleep(delay)


def _nginx_test() -> runner.CommandResult:
    try:
        return runner.run(["nginx", "-t"], timeout=COMMAND_TIMEOUT_S, check=False)
    except MissingDependencyError:
        raise MissingDependencyError(
            "nginx is configured (/etc/nginx was restored) but not installed",
            hint="install it from the Ansible playbook (or apt-get install nginx) and resume "
            "with --from-step 4",
        ) from None


def _restored_units(paths: list[SystemPath]) -> list[str]:
    units: list[str] = []
    for entry in paths:
        if not entry.restore or not (
            entry.path == SYSTEMD_DIR or entry.path.is_relative_to(SYSTEMD_DIR)
        ):
            continue
        patterns = entry.include or ["*.service", "*.timer"]
        for pattern in patterns:
            units += sorted(p.name for p in entry.path.glob(pattern) if p.is_file())
    return list(dict.fromkeys(units))


def _addresses() -> list[str]:
    try:
        result = runner.run(["hostname", "-I"], timeout=COMMAND_TIMEOUT_S, check=False)
    except DrkitError:
        return []
    return result.stdout.split()[:3] if result.ok else []


def _inventory_diff(host: HostRestore) -> dict[str, Any]:
    """Packages installed by hand on the old server versus here; noted for the operator."""
    if host.inventory is None or host.inventory.snapshot is None:
        return {}
    before = json.loads(host.restic.dump(host.inventory.snapshot, f"/{INVENTORY_FILENAME}"))
    now = collect_inventory()
    manual_before = set(before.get("manual_packages") or [])
    manual_now = set(now.get("manual_packages") or [])
    missing, added = sorted(manual_before - manual_now), sorted(manual_now - manual_before)
    diff: dict[str, Any] = {
        "os_before": (before.get("os_release") or {}).get("PRETTY_NAME"),
        "os_now": (now.get("os_release") or {}).get("PRETTY_NAME"),
        "docker_before": before.get("docker"),
        "docker_now": now.get("docker"),
        "missing": missing,
        "added": added,
    }
    if missing:
        listed = ", ".join(missing[:MAX_LISTED])
        more = f" and {len(missing) - MAX_LISTED} more" if len(missing) > MAX_LISTED else ""
        host.notes.append(
            f"Packages installed by hand on the old server, missing here ({len(missing)}): "
            f"{listed}{more}. Add those still needed to the playbook"
        )
    if diff["os_before"] != diff["os_now"]:
        host.notes.append(f"OS changed: {diff['os_before']} → {diff['os_now']}")
    return diff


def _ansible_plan(ansible: Ansible | None, skip_system: bool) -> tuple[str, ...]:
    if skip_system:
        return ("skipped (--skip-system)",)
    if ansible is None:
        return ("no playbook configured (system.ansible)",)
    lines = [
        f"{ansible.source}" + (f"@{ansible.ref}" if ansible.ref else ""),
        f"ansible-playbook -i localhost, -c local {ansible.playbook}",
    ]
    if ansible.skip_tags:
        lines.append(f"--skip-tags {','.join(ansible.skip_tags)}")
    return tuple(lines)


def _tls_plan(host: HostRestore) -> tuple[str, ...]:
    tls_config = host.manifest.system.tls
    if tls_config is None:
        return ("not configured (system.tls)",)
    if tls_config.strategy == "restore":
        return (f"restore {host.letsencrypt} from the backup",)
    return (f"certbot --standalone for the certificates nginx uses ({tls_config.email})",)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
