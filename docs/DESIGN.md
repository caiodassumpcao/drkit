# drkit — Design Specification

> Status: **Draft v1** · Owner: Caio Dassumpção Rodrigues
> This document is the technical source of truth for `drkit`. Any change in behavior must be reflected here (or in an ADR) in the same pull request.

---

## 1. Purpose

`drkit` is a CLI that backs up and **recovers an entire Linux server running Docker Compose projects** — databases, volumes, secrets, application code and server configuration — and **proves** that recovery works through automated disaster-recovery drills with measured RPO/RTO.

### 1.1 Goals

- Back up databases (PostgreSQL, MySQL/MariaDB), Docker volumes, project secrets, project state and selected system paths into a single encrypted, deduplicated, off-site repository.
- Restore at three granularities: **database**, **project**, **whole host**.
- Recover a host by **rebuilding, not cloning**: system configuration comes from Infrastructure as Code (Ansible), data comes from backups.
- Make every backup **verifiable** (integrity + real restore test) and every recovery **measurable** (step timings, RTO, RPO).
- Be safe by default: least privilege, no secrets in logs, encrypted at rest and in transit.

### 1.2 Non-goals

- Block-level / bare-metal imaging (use provider snapshots or [ReaR](https://relax-and-recover.org/)).
- Writing a storage engine — `restic` is used as the storage backend (see ADR-0001).
- Kubernetes, Windows hosts, databases not running in containers (v1).
- DNS automation (recovery prints a DNS checklist instead).

### 1.3 Scope assumptions (documented in README)

| Area | Assumption |
|---|---|
| OS | Ubuntu 22.04 / 24.04 LTS |
| Apps | Deployed with Docker Compose v2 from a git repository |
| Databases | PostgreSQL ≥ 13, MySQL ≥ 8 / MariaDB ≥ 10.6, running as Compose services |
| Proxy | Nginx on the host (or as a Compose service) |
| System config | Reproducible via an Ansible playbook (e.g. `server-hardening-kit`) |
| Privileges | `drkit` runs as root (needs Docker socket and volume mountpoints) |

---

## 2. Core concepts

| Concept | Definition |
|---|---|
| **Manifest** | `dr.yaml` — declarative description of the host: repository, retention, system, projects, notifications. |
| **Run** | One execution of `drkit backup`. Identified by `run_id` (`YYYYMMDDTHHMMSSZ-<4 hex>`). |
| **Item** | A unit backed up inside a run: `db`, `volume`, `secrets`, `project`, `system`, `meta`. |
| **Snapshot** | A restic snapshot. Each item becomes one snapshot, tagged (see §5.3). |
| **Run manifest** | JSON document (`runs/<run_id>.json`) stored as the last snapshot of a run. Lists every snapshot ID, git SHA, image digest and DB version of that run. **Restores always start from a run manifest** → consistent point-in-time set (ADR-0004). |
| **RPO** | Recovery Point Objective — max data loss = time since the last successful run. |
| **RTO** | Recovery Time Objective — time from start of restore to all healthchecks green. |

---

## 3. Architecture

```
                ┌──────────────────────────── drkit CLI (typer) ───────────────────────────┐
                │ backup · verify · restore · drill · runs · report · prune · doctor · init │
                └──────────────┬──────────────────────────────────────────────┬────────────┘
                               │                                              │
                      ┌────────▼────────┐                            ┌────────▼────────┐
                      │   Orchestrator   │  lock · steps · timings   │  Restore engine  │
                      │  (backup run)    │  hooks · run manifest     │ (step pipeline)  │
                      └────────┬────────┘                            └────────┬────────┘
             ┌─────────────────┼──────────────────┐                           │
     ┌───────▼──────┐  ┌───────▼──────┐  ┌────────▼──────┐          ┌─────────▼─────────┐
     │   Sources    │  │  restic      │  │ Notifications │          │ Ansible · Docker  │
     │ postgres     │  │  wrapper     │  │ telegram      │          │ git · nginx ·     │
     │ mysql        │  │ (subprocess) │  │ webhook       │          │ certbot · health  │
     │ volume·paths │  └───────┬──────┘  └───────────────┘          └───────────────────┘
     │ secrets      │          │
     │ project·sys  │          ▼
     └──────────────┘   S3-compatible storage (encrypted, deduplicated)
```

### 3.1 Technology decisions

| Decision | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Ops tooling standard, strong typing with mypy |
| Packaging | `uv` + `pyproject.toml` (hatchling) | Fast, reproducible lockfile |
| CLI | `typer` + `rich` | Typed CLI, readable output |
| Config | `pydantic` v2 + `PyYAML` | Validation with precise error messages |
| Logging | `structlog` | JSON when not a TTY (journald), pretty on TTY |
| HTTP | `httpx` | Notifications and healthchecks |
| Storage engine | `restic` ≥ 0.17 (external binary) | Encryption, dedup, S3, integrity check — ADR-0001 |
| Docker | `docker` / `docker compose` CLI via subprocess | No daemon SDK coupling, same behavior as operators — ADR-0003 |
| System config | `ansible-playbook` (external, optional) | Rebuild over clone — ADR-0002 |

### 3.2 Module layout

```
src/drkit/
├── __init__.py          # __version__
├── __main__.py          # python -m drkit
├── cli.py               # typer app, command wiring, exit codes
├── config.py            # pydantic models for dr.yaml, env interpolation, validation
├── errors.py            # exception hierarchy → exit codes
├── logging.py           # structlog setup (stderr), redaction processor
├── runner.py            # subprocess wrapper: timeouts, streaming pipes, secret redaction
├── lock.py              # fcntl lock on /run/drkit.lock
├── state.py             # /var/lib/drkit/state.json (last run, last verify, last drill)
├── doctor.py            # environment checks behind `drkit doctor` (results only; rendering in cli.py)
├── restic.py            # restic wrapper: init, backup (paths/stdin), dump, restore, forget, prune, check, snapshots
├── docker.py            # compose ps/images/up/down, volume resolution, exec
├── runmanifest.py       # RunManifest model, (de)serialization, lookup (latest / by id)
├── orchestrator.py      # backup run: hooks → sources → run manifest → retention → notify
├── sources/
│   ├── base.py          # Source protocol: name, kind, tags, backup(ctx) -> ItemResult
│   ├── postgres.py
│   ├── mysql.py
│   ├── volume.py
│   ├── paths.py         # system paths
│   ├── secrets.py
│   ├── project.py       # git state, compose files, image digests
│   └── system.py        # package inventory, OS info (meta only)
├── restore/
│   ├── steps.py         # Step model, pipeline, timing, resume (--from-step)
│   ├── database.py
│   ├── project.py
│   └── host.py
├── verify.py            # restic check + DB restore test in ephemeral container
├── drill.py             # local DR drill (CI)
├── health.py            # HTTP healthchecks with retry/backoff
├── notify.py            # telegram, generic webhook (Slack-compatible)
└── report.py            # JSON + Markdown reports (runs, restore, drill)
```

---

## 4. Manifest (`dr.yaml`)

Default path: `/etc/drkit/dr.yaml` (override: `--config` or `DRKIT_CONFIG`).
`${VAR}` placeholders are interpolated from the environment and from `repository.env_file`; the process environment wins on conflicts. Only the braced form is interpolated (`$VAR` is left alone, e.g. for shell hooks) and `$$` escapes a literal `$`, as in Docker Compose. `repository.env_file` itself may only use process environment variables.
The env file holds `KEY=VALUE` lines (`#` comments, optional `export` and surrounding quotes, no inline comments).
The manifest is parsed with YAML 1.2 booleans (`true`/`false` only), so keys such as `on` are never read as booleans; duplicate keys are errors.
`drkit` **refuses to run** if the manifest, password file or env file is not a regular file owned by the current user with no group/other permissions (`doctor` explains how to fix).

```yaml
version: 1
host: prod-01                         # logical host name, used in tags

repository:
  url: s3:https://s3.example.com/my-dr-bucket/prod-01
  password_file: /etc/drkit/restic.pass
  env_file: /etc/drkit/env            # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, DRKIT_* secrets

retention:                            # applied with `restic forget` after every run
  daily: 7
  weekly: 4
  monthly: 6

system:
  ansible:                            # optional; used by `restore host`
    source: https://github.com/caiodassumpcao/server-hardening-kit.git   # git URL or local path
    ref: v1.0.0
    playbook: site.yml
    extra_vars: { ssh_port: 2222 }
    skip_tags: []                     # e.g. [ufw, ssh] for CI drills
  paths:                              # backed up every run; restored after Ansible if restore: true
    - path: /etc/nginx
      restore: true
    - path: /etc/systemd/system
      include: ["app-*.service", "app-*.timer"]
      restore: true
    - path: /var/spool/cron/crontabs
      restore: true
  tls:
    strategy: reissue                 # reissue (certbot) | restore (/etc/letsencrypt from backup)
    email: ops@example.com

projects:
  - name: artesempreco
    path: /srv/artesempreco
    git:
      remote: git@github.com:caiodassumpcao/artesempreco.git
      deploy_key: /etc/drkit/keys/artesempreco      # optional, for private repos
    compose:
      project_name: artesempreco
      files: [docker-compose.yml, docker-compose.prod.yml]
    secrets: [.env]                   # paths relative to project path
    databases:
      - engine: postgres              # postgres | mysql
        service: db                   # compose service name
        database: artes
        user_env: POSTGRES_USER       # env var read from the container (default per engine)
      - engine: mysql
        service: mysql
        database: legacy
        password_env: MYSQL_PASSWORD  # mysql only: env var read from the container (default MYSQL_PASSWORD)
    volumes:
      - name: uploads                 # compose volume name (resolved via compose labels)
        consistency: live             # live | stop (stop service(s) using it during backup)
    hooks:
      pre_backup: []
      post_backup: []
      post_restore: ["docker compose exec -T app php artisan migrate --force"]
    healthchecks:
      - url: http://127.0.0.1:8080/health
        expect_status: 200
        timeout: 90                   # seconds, with exponential backoff

notifications:
  on: [failure]                       # failure | success
  telegram:
    bot_token: ${DRKIT_TG_TOKEN}
    chat_id: "123456789"
  webhook:
    url: ${DRKIT_WEBHOOK_URL}         # Slack-compatible JSON {"text": ...}
```

Validation rules (must have unit tests):
- `name` values unique and `^[a-z0-9][a-z0-9-]{1,40}$`.
- `path` absolute; `secrets` relative and must not escape the project path (`..`).
- `databases[].service` must exist in the resolved compose config (checked by `doctor`, not by parsing).
- Unknown keys are errors (`extra="forbid"`).
- Missing `${VAR}` is an error naming the variable, never printing other values.
- Errors name the file and the key path (`projects[0].databases[1].engine: ...`), report every problem at once and never echo input values.
- `retention` needs at least one of `daily`/`weekly`/`monthly`; `tls.strategy: reissue` needs `email`; `notifications` needs at least one channel.
- Database names (`databases[].database`) and volume names are unique within a project.
- `password_env` is only valid for `engine: mysql`. User and password are read from the container environment (defaults: `POSTGRES_USER`; `MYSQL_USER` + `MYSQL_PASSWORD`), so credentials never have to be duplicated in the manifest. PostgreSQL dumps authenticate over the container's local socket and need no password.

---

## 5. Backup

### 5.1 Run sequence

1. Acquire lock (`/run/drkit.lock`, override with `$DRKIT_LOCK_FILE`; non-blocking → exit code 3 if held).
2. Load + validate manifest, open the repository (`restic cat config`). If the run cannot start (repository missing or unreachable), a failure notification is sent and nothing is written.
3. Generate `run_id`, start timers.
4. For each project: `pre_backup` hooks.
5. Back up items (one restic snapshot each, see §5.2). A failing item **does not abort** the run; it is recorded as failed.
6. For each project: `post_backup` hooks (always run, even after failures, `finally` semantics).
7. Write run manifest as a `kind:meta` snapshot. If this fails the run is reported as failed: without its manifest it cannot be restored as a set.
8. Retention **per run** (ADR-0004): the policy is evaluated with `restic forget --dry-run --group-by host` over the `kind:meta` snapshots; every snapshot of each discarded run (`--tag run:<id>`) is then forgotten. The current run is never discarded. No prune — prune is a separate weekly command.
9. Update `state.json` (`$DRKIT_STATE_DIR`, default `/var/lib/drkit`), emit report, send notifications (`success` or `failure`, per `notifications.on`; delivery failures are logged, never change the exit code).
10. Exit code: `0` all ok · `1` partial failure (an item failed, or retention / state update failed).

### 5.2 Items

| Kind | How | restic call |
|---|---|---|
| `db` (postgres) | `docker exec <ctr> pg_dump -U <user> -Fc <db>` + `pg_dumpall -U <user> --globals-only` (roles); user from `$<user_env>` in the container, version from `SHOW server_version` | `restic backup --stdin --stdin-filename <project>/db/<db>.dump` (streamed, no temp file — ADR-0005), and `<db>.globals.sql` as a second snapshot of the same item (`globals_snapshot`) |
| `db` (mysql) | `docker exec -e MYSQL_PWD <ctr> mysqldump -u <user> --single-transaction --routines --triggers --events --no-tablespaces <db>`; user and password from `$<user_env>` / `$<password_env>` in the container, password handed over through drkit's environment (never argv); `mariadb-dump` / `mariadb` when the image has them (MariaDB 11) | same, `<db>.sql` |
| `volume` | resolve mountpoint via `docker volume ls --filter label=com.docker.compose.project=<p> --filter label=com.docker.compose.volume=<v>` + `docker volume inspect` | `restic backup <mountpoint>` |
| `secrets` | project-relative files | `restic backup <files>` |
| `project` | git remote, HEAD SHA, dirty flag, `git diff` patch if dirty (warning), compose files, `docker compose images --format json` digests | files + JSON |
| `system` | configured `paths` + `dpkg --get-selections`, `apt-mark showmanual`, OS release (inventory only) | paths + JSON |
| `meta` | run manifest | `--stdin-filename runs/<run_id>.json` |

**Streaming correctness:** when piping `dump → restic --stdin`, both exit codes must be checked. If the dump fails but restic succeeds, the produced snapshot is **forgotten immediately** and the item is marked failed. If the PostgreSQL globals dump fails, the database dump snapshot is forgotten too: an item is complete or absent. Covered by an integration test.

Containers are resolved by compose labels (`docker ps --filter label=com.docker.compose.project=<p> --filter label=com.docker.compose.service=<s>`), so the compose files are not needed to back up. Every restic call passes `--host <host>`, so snapshots belong to the logical host, not to the machine's hostname.

**Volume consistency:** `consistency: stop` stops only the compose services mounting the volume, backs up, then starts them again in a `finally` block.

### 5.3 Tags

Every snapshot carries: `drkit`, `host:<host>`, `run:<run_id>`, `kind:<kind>`, and when applicable `project:<name>`, `db:<name>`, `volume:<name>`. The `kind:meta` snapshot also carries `status:<ok|partial|failed>`, so `runs list` needs no manifest download.

### 5.4 Run manifest schema

```json
{
  "schema": 1,
  "run_id": "20261027T030000Z-a1f3",
  "host": "prod-01",
  "drkit_version": "0.4.0",
  "started_at": "2026-10-27T03:00:00Z",
  "finished_at": "2026-10-27T03:02:41Z",
  "status": "ok",
  "items": [
    {"kind": "db", "project": "artesempreco", "name": "artes", "engine": "postgres",
     "engine_version": "16.4", "snapshot": "3f2a9c1d…", "globals_snapshot": "8b0e44f2…",
     "bytes": 48213, "duration_s": 3.2, "status": "ok", "error": null}
  ],
  "projects": {
    "artesempreco": {"remote": "git@github.com:...", "sha": "9c1e2b7", "dirty": false,
                     "images": {"app": "ghcr.io/...@sha256:..."}}
  }
}
```

Snapshot IDs are full restic IDs. `status` is `ok` (all items ok), `partial` (some failed) or `failed` (all failed); failed items carry `error` (redacted) and no snapshot. Readers reject unknown fields and schema versions.

### 5.5 Scheduling

`examples/systemd/` ships `drkit-backup.service/.timer` (e.g. hourly), `drkit-verify.timer` (daily) and `drkit-prune.timer` (weekly), with `Persistent=true` and hardening directives (`ProtectHome`, `PrivateTmp`, `NoNewPrivileges` where compatible).

---

## 6. Verify

`drkit verify [--run latest|<id>] [--read-data-subset 5%]`

1. `restic check --read-data-subset=<n>`.
2. For each `db` item of the run: start an **ephemeral container with the same engine major version** (`postgres:<major>` / `mysql:<major>`), restore the dump into it, run sanity queries (connect, count tables > 0, optional `verify_query` from the manifest), destroy it.
3. Record result in `state.json`; notify on failure.

---

## 7. Restore

All restore commands: `--run latest|<id>` (default `latest` successful run), `--dry-run` (print plan, change nothing), `--yes` (skip confirmation), `--from-step <n>` (resume). Every step is timed; a final report is printed and saved to `/var/lib/drkit/reports/`.

### 7.1 `drkit restore db <project> [--database <name>] [--into <service>]`

1. Locate DB snapshot via run manifest.
2. Safety: take a **pre-restore dump** of the current database (tagged `kind:pre-restore`) unless `--no-safety-backup`.
3. `restic dump <snap> <file> | docker exec -i <ctr> pg_restore --clean --if-exists -d <db>` (mysql: `mysql <db>`).
4. Run sanity query.

### 7.2 `drkit restore project <project> [--to-path <dir>]`

| # | Step |
|---|---|
| 1 | Clone repo (deploy key if configured) and checkout recorded SHA; apply dirty patch if present (warn) |
| 2 | Restore compose files and secrets (`0600`, original owner) |
| 3 | `docker compose pull` (pinned digests when recorded) |
| 4 | Start only database services, wait healthy |
| 5 | Restore databases |
| 6 | Restore volumes (services stopped) |
| 7 | `docker compose up -d` |
| 8 | `post_restore` hooks |
| 9 | Healthchecks |

### 7.3 `drkit restore host [--target local] [--skip-system]`

Runs on a **fresh Ubuntu server** after `scripts/bootstrap.sh` (installs Docker, restic, git, Ansible, drkit) and after the operator places `dr.yaml`, `restic.pass` and `env` in `/etc/drkit/` (from a password manager — see §9).

| # | Step |
|---|---|
| 1 | Preflight: OS version, dependencies, repository reachable, run manifest found |
| 2 | Ansible: clone `system.ansible.source@ref`, run playbook locally (`-c local`), honoring `skip_tags` |
| 3 | Restore `system.paths` with `restore: true` |
| 4 | Reload systemd, validate `nginx -t` |
| 5 | `restore project` for each project (in manifest order) |
| 6 | TLS: `certbot` reissue or restore `/etc/letsencrypt` per `tls.strategy` |
| 7 | Reload nginx, run all healthchecks |
| 8 | Print DNS/post-recovery checklist, inventory diff (packages present before vs now), RTO report |

---

## 8. DR drill

`drkit drill --local` — end-to-end proof, designed to run on a disposable machine (GitHub Actions `ubuntu-latest` runner).

1. Start MinIO (S3) and `examples/demo-app` (Compose: PostgreSQL + MySQL + small HTTP app writing to an `uploads` volume, behind Nginx). Seed deterministic data and compute **fixtures** (row counts, checksums of uploaded files).
2. `drkit init` + `drkit backup`. Record backup end time.
3. **Disaster:** `docker compose down -v`, delete project dir, remove nginx site config, prune images.
4. `drkit restore host --skip-system` (Ansible step with `skip_tags` safe for CI, or skipped).
5. Validate: healthchecks green + fixtures identical.
6. Report: RPO (disaster time − backup end), RTO (restore start → healthy), per-step timings, data integrity result. Write `$GITHUB_STEP_SUMMARY` and upload `drill-report.json` as artifact.

Drill fails (non-zero) on any mismatch.

---

## 9. Security model

- **Encryption:** restic encrypts all data client-side (AES-256 + Poly1305). TLS in transit to S3.
- **Master secrets:** `restic.pass` + S3 credentials are the recovery keys. They **must also live outside the server** (password manager / sealed envelope). Losing them = losing the backups. README and runbook must say this prominently.
- **Least privilege:** S3 key scoped to one bucket/prefix; recommend bucket **object lock / versioning** against ransomware deleting backups (documented, not enforced).
- **File permissions:** manifest, password and env files must be regular files owned by the effective user running `drkit` (root in production) with no group/other permission bits (e.g. `0600`); `drkit` refuses otherwise. `doctor` applies the same check to deploy keys.
- **No secrets in output:** `runner.py` redacts env values and known secret patterns (`*PASSWORD*=`, `*TOKEN*=`, credentials in URLs, Telegram bot tokens) from logs, errors and reports. Values passed to commands through `env=`, the env file and secret manifest fields are registered automatically; values shorter than 6 characters are not, to avoid mangling unrelated output. Typer tracebacks never render local variables. Unit-tested.
- **Hooks** run as root from a root-owned `0600` manifest; documented as trusted input.
- **Supply chain:** pinned dependencies (`uv.lock`), Dependabot, `pip-audit`, Gitleaks, Trivy on the Docker image, signed tags and releases.

---

## 10. Observability

- Logs: structlog JSON (`event`, `run_id`, `item`, `duration_s`, `status`) to stderr → journald (human-readable on a TTY). stdout is reserved for command output, so `--json` stays machine-parseable. Every log value passes through the redactor.
- `state.json`: last run/verify/drill status and timestamps.
- `drkit report [--format md|json]`: last N runs, sizes, durations, current RPO, last verify, last drill RTO.
- Exit codes: `0` ok · `1` partial failure · `2` config error · `3` lock held · `4` missing dependency · `5` restore aborted. `doctor` reports the most important failure: config/permissions (`2`), then dependencies (`4`), then repository access (`1`).
- Environment overrides (tests, unprivileged development): `DRKIT_CONFIG`, `DRKIT_STATE_DIR`, `DRKIT_LOCK_FILE`.

---

## 11. CLI reference

| Command | Description |
|---|---|
| `drkit doctor` | Check dependencies, permissions, manifest, repository reachability, compose services |
| `drkit init` | Initialize restic repository |
| `drkit backup [--project p] [--only db,volume,...]` | Run a backup |
| `drkit runs list [--limit n]` / `drkit runs show <id>` | List / inspect runs |
| `drkit verify [--run id]` | Integrity + DB restore test |
| `drkit restore db\|project\|host ...` | Restore (see §7) |
| `drkit drill --local` | DR drill |
| `drkit prune` | `restic prune` (weekly) |
| `drkit report` | Status report |
| Global | `--config`, `--json` (machine output), `--verbose`, `--version` |

---

## 12. Testing strategy

| Level | Tooling | Scope |
|---|---|---|
| Unit | pytest, ≥ 85 % coverage on non-IO modules | config validation, tag/command building, redaction, run manifest, retention args, step pipeline |
| Integration | pytest marker `integration`, Docker Compose fixtures (`tests/integration/compose.yaml`: MinIO via the community `pgsty/minio` build since upstream images are no longer published, PostgreSQL, MySQL, MariaDB), run with `--no-cov` in CI | each source backs up and restores; dump-failure handling; retention; volume consistency |
| End-to-end | `drkit drill --local` in CI | full host recovery |
| Static | ruff (lint + format), mypy `--strict` | everything |

---

## 13. CI/CD (GitHub Actions)

- `ci.yml` (push/PR): ruff, mypy, unit tests + coverage, integration tests, build wheel + Docker image, Gitleaks, pip-audit, Trivy image scan.
- `drill.yml` (push to `main` + weekly cron + manual): DR drill, publishes summary + artifact; status badge in README.
- `release.yml` (tag `v*`): build, GitHub Release with notes from `CHANGELOG.md`, attach wheel.
- Dependabot: pip (uv), GitHub Actions, Docker.

---

## 14. Repository structure

```
drkit/
├── .github/
│   ├── workflows/ (ci.yml, drill.yml, release.yml)
│   ├── ISSUE_TEMPLATE/ (bug_report.yml, feature_request.yml)
│   ├── pull_request_template.md
│   └── dependabot.yml
├── docs/
│   ├── DESIGN.md
│   ├── configuration.md       # full dr.yaml reference
│   ├── runbook.md             # step-by-step disaster recovery for humans
│   └── adr/ (0001-…md)
├── examples/
│   ├── dr.yaml
│   ├── demo-app/              # used by drill
│   └── systemd/
├── scripts/bootstrap.sh
├── src/drkit/ …
├── tests/ (unit/, integration/, fixtures/)
├── Dockerfile
├── pyproject.toml · uv.lock
├── .gitattributes · .gitignore · .editorconfig · .pre-commit-config.yaml
├── CHANGELOG.md · CONTRIBUTING.md · SECURITY.md · LICENSE
└── README.md
```

---

## 15. Roadmap

Each phase = one branch, one PR, one release tag. A phase is done only when **all** acceptance criteria pass and CI is green.

### Phase 0 — Foundation (`chore/foundation`, no tag)
- Project scaffold (§14), `pyproject.toml`, `uv.lock`, ruff/mypy/pytest config, pre-commit, `.gitattributes` (`* text=auto eol=lf`).
- `config.py` with full manifest models + validation tests; `examples/dr.yaml`.
- `runner.py` (timeouts, pipes, redaction), `logging.py`, `errors.py`, `lock.py`.
- CLI skeleton: `--version`, `doctor` (deps + permissions + manifest).
- CI: lint, types, unit tests, Gitleaks. Dependabot, issue/PR templates, `SECURITY.md`, `CONTRIBUTING.md`, `LICENSE`, `CHANGELOG.md`.
- ADR-0001 (restic), ADR-0002 (rebuild over image), ADR-0003 (Docker CLI via subprocess).

**Done when:** `uv run drkit --help` and `drkit doctor` work; invalid manifests produce precise errors; CI green.

### Phase 1 — Database backup (`feat/database-backup`, `v0.1.0`)
- `restic.py` (init, backup paths/stdin, snapshots, forget), `docker.py` (compose service → container).
- `sources/postgres.py`, `sources/mysql.py` with streaming.
- `orchestrator.py`, run manifest (ADR-0004), streaming (ADR-0005), tags, retention, `state.json`.
- `notify.py` (Telegram, webhook), `drkit init`, `drkit backup`, `drkit runs list/show`.
- `examples/systemd/` backup timer.
- Integration tests: MinIO + Postgres + MySQL; failed dump → snapshot forgotten, run status `partial`.

**Done when:** a backup of both engines lands in MinIO with correct tags and run manifest; retention applied; failure notification delivered (mocked in tests).

### Phase 2 — Files, volumes & verification (`feat/files-volumes-verify`, `v0.2.0`)
- Sources: `volume` (+ `consistency: stop`), `secrets`, `project`, `paths`, `system` inventory.
- Hooks (`pre_backup`, `post_backup` with finally semantics).
- `verify` (restic check + ephemeral DB restore), `prune`, `report`, verify/prune timers.
- ADR-0006 (secrets & master key handling).

**Done when:** a full run contains all item kinds; verify restores each DB into an ephemeral container and passes; stopped services always restart (tested).

### Phase 3 — Project restore (`feat/project-restore`, `v0.3.0`)
- `restore/steps.py` (pipeline, timings, `--dry-run`, `--from-step`), `health.py`.
- `restore db` (with pre-restore safety dump) and `restore project` (§7.2).

**Done when:** integration test destroys a demo project (containers, volumes, directory) and `restore project` brings it back with identical data and green healthcheck.

### Phase 4 — Host restore (`feat/host-restore`, `v0.4.0`)
- `scripts/bootstrap.sh` (idempotent, `set -euo pipefail`, shellcheck clean).
- `restore host` (§7.3): Ansible integration, system paths, nginx validation, TLS strategy, checklist, inventory diff.
- `docs/runbook.md`.

**Done when:** on a clean Ubuntu VM (or CI runner) `bootstrap.sh` + `restore host` recover all projects and nginx; report shows per-step timings.

### Phase 5 — DR drill & v1.0 (`feat/dr-drill`, `v1.0.0`)
- `examples/demo-app`, `drill.py`, `drill.yml` (push + weekly), step summary + artifact, README badge.
- `docs/configuration.md` complete; README "Measured results" filled with real drill numbers.
- Dockerfile + Trivy, `release.yml`, CHANGELOG for 1.0.0.

**Done when:** drill passes in CI with fixtures identical, RTO/RPO published, release `v1.0.0` created.

---

## 16. ADR index

| # | Title |
|---|---|
| 0001 | Use restic as storage engine |
| 0002 | Recover hosts by rebuilding (IaC + data) instead of imaging |
| 0003 | Drive Docker through its CLI instead of the SDK |
| 0004 | Run manifest as the unit of consistent restore |
| 0005 | Stream database dumps to restic via stdin |
| 0006 | Secrets and master key handling |

ADR format: Context · Decision · Alternatives considered · Consequences.
