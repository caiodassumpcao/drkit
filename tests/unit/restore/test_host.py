import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from drkit import doctor, health, runner
from drkit.config import Ansible, LoadedConfig, Manifest
from drkit.doctor import Category, Check, Status
from drkit.errors import CommandError, DrkitError, MissingDependencyError
from drkit.health import HealthResult
from drkit.restic import Restic, Snapshot
from drkit.restore import host as host_module
from drkit.restore import project as project_restore
from drkit.restore import steps, tls
from drkit.restore.steps import RestorePlan, Step
from drkit.runmanifest import ProjectState, RunManifest
from drkit.runner import CommandResult
from drkit.sources.base import ItemResult

RUN_ID = "20261029T030000Z-a1f3"
T0 = datetime(2026, 10, 29, 3, tzinfo=UTC)
PATHS = ItemResult(kind="system", name="paths", snapshot="paths1", status="ok")
INVENTORY = ItemResult(kind="system", name="inventory", snapshot="inv1", status="ok")
FILES = ItemResult(kind="project", project="shop", name="shop", snapshot="files1", status="ok")


def manifest(tmp_path: Path, **system: Any) -> LoadedConfig:
    data = {
        "version": 1,
        "host": "prod-01",
        "repository": {"url": "s3:https://s3.example.com/b", "password_file": "/pw"},
        "retention": {"daily": 7},
        "system": {
            "paths": [
                {"path": str(tmp_path / "nginx"), "restore": True},
                {"path": str(tmp_path / "letsencrypt")},
            ],
            "tls": {"strategy": "restore"},
            **system,
        },
        "projects": [
            {
                "name": "shop",
                "path": str(tmp_path / "srv" / "shop"),
                "git": {"remote": "git@example.com:shop.git"},
                "compose": {"files": ["compose.yaml"]},
                "healthchecks": [{"url": "http://127.0.0.1/health"}],
            }
        ],
    }
    return LoadedConfig(path=Path("/etc/drkit/dr.yaml"), manifest=Manifest.model_validate(data))


def run_with(*items: ItemResult) -> RunManifest:
    return RunManifest(
        run_id=RUN_ID,
        host="prod-01",
        drkit_version="0.4.0",
        started_at=T0,
        finished_at=T0,
        status="ok",
        items=list(items),
        projects={"shop": ProjectState(sha="9c1e2b7d" * 5)},
    )


@dataclass
class FakeRestic:
    snapshot_paths: list[str]
    restored: list[tuple[str, str, str]] = field(default_factory=list)
    documents: dict[str, str] = field(default_factory=dict)
    ready: int = 0

    def ensure_ready(self) -> None:
        self.ready += 1

    def snapshot(self, snapshot_id: str) -> Snapshot:
        return Snapshot(snapshot_id, snapshot_id, T0, "prod-01", (), tuple(self.snapshot_paths))

    def restore(self, snapshot_id: str, path: str, target: str, *, delete: bool = False) -> None:
        self.restored.append((snapshot_id, path, target))
        Path(target, "restored").write_text("x")

    def dump(self, snapshot_id: str, path: str) -> str:
        return self.documents[path]


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LoadedConfig, FakeRestic, list[RestorePlan]]:
    """A manifest, a repository and stand-ins for the per-project restores."""
    monkeypatch.setattr(tls, "NGINX_DIR", tmp_path / "nginx")
    monkeypatch.setattr(tls, "LETSENCRYPT", tmp_path / "letsencrypt")
    monkeypatch.setattr(host_module, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(host_module, "REBOOT_REQUIRED", tmp_path / "reboot-required")
    loaded = manifest(tmp_path)
    restic = FakeRestic(snapshot_paths=[str(tmp_path / "nginx"), str(tmp_path / "letsencrypt")])
    projects: list[RestorePlan] = []

    def plan_for_run(loaded: LoadedConfig, project: Any, restic: Any, run: Any, **kw: Any) -> Any:
        assert kw["healthchecks_deferred"] is True
        project_plan = RestorePlan(
            kind="project",
            target=project.name,
            run_id=run.run_id,
            steps=[Step("Clone", lambda: "cloned"), Step("Start", lambda: "started")],
            warnings=["the checkout had local changes"],
            restore_id=kw["restore_id"],
        )
        projects.append(project_plan)
        return project_plan

    monkeypatch.setattr(project_restore, "plan_for_run", plan_for_run)
    monkeypatch.setattr(
        host_module, "find_run", lambda restic, run_id, accept, what: _accepted(accept)
    )
    return loaded, restic, projects


def _accepted(accept: Any) -> RunManifest:
    run = run_with(FILES, PATHS, INVENTORY)
    assert accept(run)
    assert not accept(run_with(FILES))  # system.paths missing
    return run


def host_plan(setup: Any, **kwargs: Any) -> RestorePlan:
    loaded, restic, _ = setup
    return host_module.plan(loaded, cast(Restic, restic), **kwargs)


def test_plan_has_the_eight_steps(setup: Any) -> None:
    restore_plan = host_plan(setup)
    _, _, projects = setup
    assert (restore_plan.kind, restore_plan.target, restore_plan.run_id) == (
        "host",
        "prod-01",
        RUN_ID,
    )
    assert [step.title for step in restore_plan.steps] == [
        "Preflight",
        "Provision the host with Ansible",
        "Restore system paths",
        "Reload systemd and validate nginx",
        "Restore the projects",
        "TLS certificates",
        "Reload nginx and run all healthchecks",
        "Recovery checklist and report",
    ]
    assert restore_plan.steps[1].plan == ("no playbook configured (system.ansible)",)
    assert restore_plan.steps[4].plan == ("shop: 2 steps (healthchecks run at step 7)",)
    assert restore_plan.steps[6].plan[-1] == "GET http://127.0.0.1/health → 200"
    assert restore_plan.warnings == ["shop: the checkout had local changes"]
    assert [p.restore_id for p in projects] == [restore_plan.restore_id]
    assert "1 system path(s) and 1 project(s)" in restore_plan.summary


def test_tls_restore_needs_letsencrypt_in_the_backup(
    setup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, restic, _ = setup
    loaded = manifest(tmp_path, paths=[{"path": str(tmp_path / "nginx"), "restore": True}])
    with pytest.raises(DrkitError, match=r"tls.strategy is 'restore' but .* not in system.paths"):
        host_module.plan(loaded, cast(Restic, restic))


def test_failed_paths_item(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    failed = PATHS.model_copy(update={"status": "failed", "snapshot": None})
    monkeypatch.setattr(
        host_module, "find_run", lambda restic, run_id, accept, what: run_with(FILES, failed)
    )
    with pytest.raises(DrkitError, match=f"run {RUN_ID} has no backup of system.paths"):
        host_plan(setup)


# --------------------------------------------------------------------------- steps


def host_state(setup: Any, **kwargs: Any) -> host_module.HostRestore:
    loaded, restic, _ = setup
    plan = host_plan(setup, **kwargs)
    # The plan's steps close over their HostRestore; rebuild an equivalent one to call steps.
    return host_module.HostRestore(
        loaded=loaded,
        restic=cast(Restic, restic),
        run=run_with(FILES, PATHS, INVENTORY),
        restore_id=plan.restore_id,
        projects=setup[2],
        skip_system=kwargs.get("skip_system", False),
        paths=PATHS,
        inventory=INVENTORY,
    )


class TestPreflight:
    @pytest.fixture
    def deps(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
        checked: list[str] = []
        missing: list[str] = []

        def check(dependency: doctor.Dependency, *, required: bool = True) -> Check:
            checked.append(dependency.name)
            if dependency.name in missing:
                return Check(Category.DEPENDENCIES, dependency.name, Status.FAIL, "not found")
            return Check(Category.DEPENDENCIES, dependency.name, Status.OK, "1.0.0")

        monkeypatch.setattr(doctor, "check_dependency", check)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        release = tmp_path / "os-release"
        release.write_text('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n')
        monkeypatch.setattr(host_module, "OS_RELEASE", release)
        self.missing = missing
        return checked

    def test_ok(self, setup: Any, deps: list[str]) -> None:
        host = host_state(setup)
        detail = host_module.preflight(host)
        assert detail.startswith("Ubuntu 24.04; restic 1.0.0, docker 1.0.0")
        assert detail.endswith(f"run {RUN_ID}")
        assert deps == ["restic", "docker", "docker compose", "git"]
        assert setup[1].ready == 1
        assert host.notes == []

    def test_ansible_is_needed_unless_skipped(
        self, setup: Any, deps: list[str], tmp_path: Path
    ) -> None:
        _, restic, projects = setup
        ansible = manifest(tmp_path, ansible={"source": "https://example.com/pb.git"})
        host = host_state((ansible, restic, projects))
        host_module.preflight(host)
        assert deps[-1] == "ansible-playbook"
        deps.clear()
        host.skip_system = True
        host_module.preflight(host)
        assert "ansible-playbook" not in deps

    def test_missing_dependency(self, setup: Any, deps: list[str]) -> None:
        self.missing.append("restic")
        with pytest.raises(MissingDependencyError, match="restic: not found") as exc:
            host_module.preflight(host_state(setup))
        assert exc.value.hint is not None
        assert "bootstrap.sh" in exc.value.hint

    def test_unsupported_release_is_noted(
        self, setup: Any, deps: list[str], tmp_path: Path
    ) -> None:
        (tmp_path / "os-release").write_text(
            'NAME="Debian GNU/Linux"\nID=debian\nVERSION_ID="12"\n'
        )
        host = host_state(setup)
        host_module.preflight(host)
        assert host.notes[0].startswith("Debian GNU/Linux 12 is not a supported release")

    def test_needs_root(self, setup: Any, deps: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        with pytest.raises(DrkitError, match="restore host must run as root"):
            host_module.preflight(host_state(setup))


class TestProvision:
    @pytest.fixture
    def playbook(self, tmp_path: Path) -> Path:
        """A playbook repository with a tag, as git would serve it."""
        repo = tmp_path / "playbook-repo"
        (repo / "playbooks").mkdir(parents=True)
        (repo / "playbooks" / "site.yml").write_text("- hosts: localhost\n")
        (repo / "requirements.yml").write_text("collections: []\n")
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "dev@example.com"],
            ["config", "user.name", "Dev"],
            ["config", "commit.gpgsign", "false"],
            ["add", "."],
            ["commit", "-q", "-m", "v1"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/tags/v1.1.0", "HEAD"], check=True
        )
        return repo

    @pytest.fixture
    def commands(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        real_run = runner.run
        self.galaxy_failures = 0
        self.playbook_code = 0

        def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
            if args[0] == "git":
                return real_run(args, **kwargs)
            calls.append({"args": list(args), **kwargs})
            if args[0] == "ansible-galaxy" and self.galaxy_failures:
                self.galaxy_failures -= 1
                return CommandResult(tuple(args), 1, "", "HTTP 502 Bad Gateway", 0.0)
            out = "PLAY RECAP\nlocalhost : ok=120 changed=45 unreachable=0 failed=0 skipped=9\n"
            code = self.playbook_code if args[0] == "ansible-playbook" else 0
            err = "TASK [ssh] fatal: boom" if code else ""
            return CommandResult(tuple(args), code, out, err, 0.0)

        monkeypatch.setattr(runner, "run", run)
        monkeypatch.setattr(host_module, "sleep", lambda seconds: None)
        return calls

    def ansible(self, playbook: Path, **fields: Any) -> Ansible:
        return Ansible.model_validate(
            {
                "source": str(playbook),
                "ref": "v1.1.0",
                "playbook": "playbooks/site.yml",
                "extra_vars": {"hardening_target": "localhost", "token": "s3cret-value"},
                "skip_tags": ["ssh", "ufw"],
                **fields,
            }
        )

    def test_runs_the_playbook_locally(
        self, playbook: Path, tmp_path: Path, commands: list[dict[str, Any]]
    ) -> None:
        self.galaxy_failures = 1
        work = tmp_path / "work"
        work.mkdir()
        detail = host_module.run_playbook(self.ansible(playbook), work, tmp_path / "ansible.log")

        assert detail == (
            f"playbooks/site.yml from {playbook}@v1.1.0: ok=120 changed=45 unreachable=0, "
            f"skipped tags ssh, ufw (log: {tmp_path / 'ansible.log'})"
        )
        galaxy, galaxy_retry, ansible = commands
        assert (
            galaxy["args"]
            == galaxy_retry["args"]
            == [
                "ansible-galaxy",
                "install",
                "-r",
                "requirements.yml",
            ]
        )
        assert ansible["args"] == [
            *("ansible-playbook", "-i", "localhost,", "-c", "local"),
            *("-e", f"@{work / 'extra-vars.json'}", "--skip-tags", "ssh,ufw"),
            str(work / "playbook" / "playbooks" / "site.yml"),
        ]
        assert ansible["cwd"] == work / "playbook"
        assert ansible["plain_env"]["ANSIBLE_LOG_PATH"] == str(tmp_path / "ansible.log")
        # Extra vars may hold secrets: in a private file, never on the command line.
        extra_vars = work / "extra-vars.json"
        assert json.loads(extra_vars.read_text())["token"] == "s3cret-value"
        assert extra_vars.stat().st_mode & 0o777 == 0o600

    def test_galaxy_keeps_failing(
        self, playbook: Path, tmp_path: Path, commands: list[dict[str, Any]]
    ) -> None:
        self.galaxy_failures = 3
        with pytest.raises(CommandError, match="ansible-galaxy could not install"):
            host_module.run_playbook(self.ansible(playbook), tmp_path, tmp_path / "a.log")

    def test_failed_playbook(
        self, playbook: Path, tmp_path: Path, commands: list[dict[str, Any]]
    ) -> None:
        self.playbook_code = 2
        with pytest.raises(CommandError, match="ansible-playbook failed with exit code 2") as exc:
            host_module.run_playbook(self.ansible(playbook), tmp_path, tmp_path / "a.log")
        assert exc.value.hint is not None
        assert str(tmp_path / "a.log") in exc.value.hint

    def test_plain_directory_and_missing_playbook(
        self, tmp_path: Path, commands: list[dict[str, Any]]
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "site.yml").write_text("- hosts: localhost\n")
        ansible = Ansible.model_validate({"source": str(plain)})
        assert host_module.run_playbook(ansible, tmp_path, tmp_path / "a.log").startswith(
            f"site.yml from {plain}: ok=120"
        )
        assert commands[0]["cwd"] == plain
        missing = Ansible.model_validate({"source": str(plain), "playbook": "nope.yml"})
        with pytest.raises(DrkitError, match=r"playbook nope.yml not found"):
            host_module.run_playbook(missing, tmp_path, tmp_path / "a.log")

    def test_step(
        self,
        setup: Any,
        playbook: Path,
        tmp_path: Path,
        commands: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DRKIT_STATE_DIR", str(tmp_path / "state"))
        _, restic, projects = setup
        with_ansible = manifest(
            tmp_path, ansible={"source": str(playbook), "playbook": "playbooks/site.yml"}
        )
        host = host_state((with_ansible, restic, projects))
        assert "ok=120" in host_module.provision(host)
        assert (tmp_path / "state" / "reports").is_dir()
        host.skip_system = True
        assert host_module.provision(host) == "skipped (--skip-system)"
        assert host_module.provision(host_state(setup)) == "no playbook configured (system.ansible)"


class TestSystem:
    @pytest.fixture
    def commands(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []
        self.nginx: tuple[int, str] = (0, "nginx: configuration file test is successful")

        def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
            calls.append(list(args))
            if args[0] == "nginx":
                code, err = self.nginx
                return CommandResult(tuple(args), code, "", err, 0.0)
            return CommandResult(tuple(args), 0, "10.0.0.5 172.17.0.1\n", "", 0.0)

        monkeypatch.setattr(runner, "run", run)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        return calls

    def test_restore_paths(self, setup: Any, tmp_path: Path) -> None:
        host = host_state(setup)
        assert host_module.restore_paths(host) == f"1 path(s) restored: {tmp_path / 'nginx'}"
        assert setup[1].restored == [("paths1", str(tmp_path / "nginx"), str(tmp_path / "nginx"))]

    def test_path_missing_from_the_snapshot(self, setup: Any) -> None:
        setup[1].snapshot_paths = ["/etc/other"]
        with pytest.raises(DrkitError, match=r"is not in snapshot paths1 of system.paths"):
            host_module.restore_paths(host_state(setup))

    def test_reload_and_validate(
        self, setup: Any, tmp_path: Path, commands: list[list[str]]
    ) -> None:
        host = host_state(setup)
        assert host_module.reload_system(host) == "systemd reloaded; no nginx configuration"
        (tmp_path / "nginx").mkdir()
        (tmp_path / "nginx" / "nginx.conf").write_text("events {}\n")
        assert host_module.reload_system(host) == "systemd reloaded; nginx configuration valid"
        assert ["systemctl", "daemon-reload"] in commands

        self.nginx = (1, 'nginx: [emerg] unknown directive "proxy_passs"')
        with pytest.raises(CommandError, match=r"nginx -t failed: .*proxy_passs"):
            host_module.reload_system(host)

    def test_certificates_issued_later(
        self, setup: Any, tmp_path: Path, commands: list[list[str]]
    ) -> None:
        _, restic, projects = setup
        reissue = manifest(tmp_path, tls={"strategy": "reissue", "email": "ops@example.com"})
        host = host_state((reissue, restic, projects))
        (tmp_path / "nginx").mkdir()
        (tmp_path / "nginx" / "nginx.conf").write_text("events {}\n")
        self.nginx = (1, 'nginx: [emerg] cannot load certificate "/etc/letsencrypt/live/x/f.pem"')
        assert host_module.reload_system(host).endswith(
            "nginx configuration waits for its certificates (issued in step 6)"
        )
        assert host_module.reload_system(host_state(setup)).endswith(
            "nginx configuration waits for its certificates (restored in step 6)"
        )
        no_tls = host_state((manifest(tmp_path, tls=None), restic, projects))
        with pytest.raises(CommandError, match="nginx -t failed"):
            host_module.reload_system(no_tls)

    def test_nginx_not_installed(
        self, setup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
            if args[0] == "nginx":
                raise MissingDependencyError("command not found: nginx")
            return CommandResult(tuple(args), 0, "", "", 0.0)

        monkeypatch.setattr(runner, "run", run)
        (tmp_path / "nginx").mkdir()
        (tmp_path / "nginx" / "nginx.conf").write_text("events {}\n")
        with pytest.raises(MissingDependencyError, match=r"nginx is configured .* not installed"):
            host_module.reload_system(host_state(setup))

    def test_tls(
        self, setup: Any, tmp_path: Path, commands: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = host_state(setup)
        (tmp_path / "letsencrypt" / "live" / "shop.example.com").mkdir(parents=True)
        assert host_module.restore_tls(host) == "1 certificate(s) restored: shop.example.com"

        _, restic, projects = setup
        assert (
            host_module.restore_tls(host_state((manifest(tmp_path, tls=None), restic, projects)))
            == "not configured (system.tls)"
        )

        reissue = manifest(tmp_path, tls={"strategy": "reissue", "email": "ops@example.com"})
        issued: list[str] = []

        def fake_reissue(email: str, nginx: Path, live: Path) -> str:
            issued.append(email)
            return "1"

        monkeypatch.setattr(tls, "reissue", fake_reissue)
        ok = Check(Category.DEPENDENCIES, "certbot", Status.OK, "2.9.0")
        monkeypatch.setattr(doctor, "check_dependency", lambda dependency: ok)
        assert host_module.restore_tls(host_state((reissue, restic, projects))) == "1"
        assert issued == ["ops@example.com"]
        missing = Check(Category.DEPENDENCIES, "certbot", Status.FAIL, "not found in PATH")
        monkeypatch.setattr(doctor, "check_dependency", lambda dependency: missing)
        with pytest.raises(MissingDependencyError, match="certbot: not found in PATH"):
            host_module.restore_tls(host_state((reissue, restic, projects)))

    def test_start_and_check(
        self, setup: Any, tmp_path: Path, commands: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = [HealthResult("http://127.0.0.1/health", True, 1, 0.1, 200)]
        monkeypatch.setattr(health, "wait_all", lambda checks: results)
        (tmp_path / "nginx").mkdir()
        (tmp_path / "nginx" / "nginx.conf").write_text("events {}\n")
        host = host_state(setup)
        assert host_module.start_and_check(host) == "nginx running; 1 healthcheck(s) green"
        assert ["systemctl", "enable", "--now", "nginx"] in commands
        assert ["systemctl", "reload", "nginx"] in commands
        assert host.healthy_at is not None

        results[0] = HealthResult("http://127.0.0.1/health", False, 9, 60.0, 502, "HTTP 502")
        with pytest.raises(DrkitError, match=r"http://127.0.0.1/health: HTTP 502"):
            host_module.start_and_check(host)
        self.nginx = (1, "nginx: [emerg] bad")
        with pytest.raises(CommandError, match="nginx -t failed"):
            host_module.start_and_check(host)

    def test_checklist(
        self, setup: Any, tmp_path: Path, commands: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, restic, projects = setup
        units = tmp_path / "systemd"
        units.mkdir()
        (units / "app-worker.service").write_text("[Service]\n")
        (units / "app-cleanup.timer").write_text("[Timer]\n")
        with_units = manifest(
            tmp_path,
            paths=[
                {"path": str(tmp_path / "nginx"), "restore": True},
                {"path": str(tmp_path / "letsencrypt")},
                {"path": str(units), "include": ["app-*.service", "app-*.timer"], "restore": True},
            ],
        )
        (tmp_path / "nginx" / "sites-enabled").mkdir(parents=True)
        (tmp_path / "nginx" / "sites-enabled" / "shop").write_text(
            "server { server_name shop.example.com; }"
        )
        (tmp_path / "reboot-required").write_text("")
        restic.documents["/system/inventory.json"] = json.dumps(
            {
                "os_release": {"PRETTY_NAME": "Ubuntu 22.04.5 LTS"},
                "docker": "27.0.0",
                "manual_packages": ["nginx", "htop", *(f"pkg{i}" for i in range(20))],
            }
        )
        monkeypatch.setattr(
            host_module,
            "collect_inventory",
            lambda: {
                "os_release": {"PRETTY_NAME": "Ubuntu 24.04.1 LTS"},
                "docker": "29.8.0",
                "manual_packages": ["nginx", "jq"],
            },
        )
        host = host_state((with_units, restic, projects))
        host.started, host.healthy_at = 100.0, 342.5

        detail = host_module.checklist(host)

        assert (
            detail
            == "RTO 242.5s; packages: 21 missing, 1 new since the backup; 8 checklist item(s)"
        )
        assert host.facts["rto_s"] == 242.5
        assert host.facts["inventory"]["added"] == ["jq"]
        assert host.notes[0] == (
            "DNS: point shop.example.com to this server (10.0.0.5, 172.17.0.1); lower the TTL "
            "first if the old records are still cached"
        )
        assert "no longer backs up as prod-01" in host.notes[1]
        assert host.notes[2].startswith(
            "Units restored but not enabled by drkit: app-worker.service, app-cleanup.timer"
        )
        assert host.notes[3] == "TLS: check that renewal works (`certbot renew --dry-run`)"
        assert host.notes[4].startswith("A reboot is pending")
        assert host.notes[6].startswith(
            "Packages installed by hand on the old server, missing here (21): htop, pkg0"
        )
        assert "and 6 more" in host.notes[6]
        assert host.notes[7] == "OS changed: Ubuntu 22.04.5 LTS → Ubuntu 24.04.1 LTS"


def test_projects_are_restored_with_their_own_steps(setup: Any) -> None:
    host = host_state(setup)
    outcome = host_module.restore_projects(host)
    assert isinstance(outcome, steps.Outcome)
    [shop] = outcome.substeps
    assert (shop.title, shop.status, shop.detail) == ("Project shop", "ok", "2 steps")
    assert [s.title for s in shop.substeps] == ["Clone", "Start"]
    assert outcome.detail is not None
    assert outcome.detail.startswith("shop (")


def test_a_failed_project_stops_the_host_restore(setup: Any) -> None:
    host = host_state(setup)

    def broken() -> str:
        raise DrkitError("pg_restore failed")

    host.projects[0].steps[1] = Step("Start", broken)
    with pytest.raises(steps.NestedStepError) as exc:
        host_module.restore_projects(host)
    assert exc.value.message == "project shop stopped at its step 2 (Start): pg_restore failed"
    assert exc.value.hint is not None
    assert f"--run {RUN_ID} --from-step 2" in exc.value.hint
    assert [s.status for s in exc.value.substeps[0].substeps] == ["ok", "failed"]


def test_full_plan_runs_and_measures_rto(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_plan = host_plan(setup)
    for name in ("preflight", "provision", "restore_paths", "reload_system", "restore_tls"):
        monkeypatch.setattr(host_module, name, lambda host, name=name: name)
    monkeypatch.setattr(health, "wait_all", lambda checks: [])
    monkeypatch.setattr(host_module, "collect_inventory", lambda: {})
    monkeypatch.setattr(host_module, "_addresses", lambda: [])
    setup[1].documents["/system/inventory.json"] = "{}"

    report = steps.execute(restore_plan)

    assert report.ok, report.steps
    assert report.steps[4].substeps[0].title == "Project shop"
    assert report.facts["rto_s"] >= 0
    assert report.notes[0].startswith("DNS: point your domains to this server")
    assert report.to_dict()["steps"][4]["substeps"][0]["substeps"][0]["title"] == "Clone"
