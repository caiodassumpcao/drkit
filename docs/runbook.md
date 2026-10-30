# Recovery runbook

Step-by-step procedures for bringing data and services back with drkit. Each procedure says
what you need, what to run, how to check the result and how to undo it. Commands assume a
root shell on the server (`sudo -i`).

| Situation | Procedure |
|---|---|
| A table was dropped, a migration went wrong, rows were deleted | [Restore a database](#restore-a-database) |
| A bad deploy, a corrupted volume, a deleted project directory | [Restore a project](#restore-a-project) |
| The server is gone | [Restore a host](#restore-a-host) |

## Before you start

- **Keys.** `/etc/drkit/dr.yaml`, `/etc/drkit/restic.pass` and `/etc/drkit/env` must be in
  place. On a server that lost them, copy them back from the password manager first: without
  `restic.pass` and the storage credentials the backups cannot be read.
- **Health of the setup.** Run `drkit doctor`. Every check must pass; for a project restore,
  `compose files` and `git remote` matter most (the code is cloned again, with the deploy key).
- **No backup in the way.** A restore holds the run lock. If a backup or prune is running,
  drkit exits with code `3`: wait for it, or stop the timer for the duration
  (`systemctl stop drkit-backup.timer`, and `start` it again afterwards).

## Choose the restore point

Restores always start from a **run**: one consistent set of snapshots (ADR-0004).

```bash
drkit runs list               # newest first, with status
drkit runs show <run_id>      # items, snapshot IDs, commit and images of a run
```

By default (`--run latest`) drkit takes the newest run with status `ok` that contains what you
restore. To go back further, for example to the last run before the bad migration, pass
`--run <run_id>`. Note the run ID: a resumed restore must use the same one, and new backups
keep moving `latest` forward.

## Restore a database

Use this when the data is wrong but the service is fine. Only the database goes back in time:
code, files and other databases are untouched, and PostgreSQL roles keep their current
passwords.

1. **See the plan.**

   ```bash
   drkit restore db shop --dry-run                      # every database of project shop
   drkit restore db shop --database orders --dry-run    # just one
   ```

   Check the run ID, the snapshot and the target service of each database.

2. **Restore.**

   ```bash
   drkit restore db shop --database orders --run <run_id>
   ```

   drkit asks for confirmation (use `--yes` in scripts), then:

   1. checks that the dumps exist and the services run;
   2. dumps each current database into a **safety snapshot** (`kind:pre-restore`), so the
      restore can be undone;
   3. restores each dump; PostgreSQL does it in a single transaction, so a failure leaves the
      database exactly as it was;
   4. checks that the database has tables (and runs `verify_query`, if configured).

   Tables in the dump are replaced; objects created after the backup are left in place.

3. **Check.** Look at the data the incident was about, and at the application. The report is
   in `/var/lib/drkit/reports/restore-<restore_id>.json`.

### Restore into a copy first

To inspect a run before touching production, add a database service to the project (for
example `db-staging`, with the same image and credentials) and restore into it:

```bash
drkit restore db shop --database orders --into db-staging
```

### Undo a database restore

The safety dump holds the state the restore replaced. Find it by the restore ID printed at the
start (also in the report):

```bash
set -a; . /etc/drkit/env; set +a    # storage credentials, exported for plain restic
export RESTIC_REPOSITORY=<repository.url> RESTIC_PASSWORD_FILE=/etc/drkit/restic.pass
restic snapshots --tag kind:pre-restore --tag restore:<restore_id>
```

PostgreSQL (custom format dump):

```bash
restic dump <snapshot> /shop/db/orders.dump \
  | docker exec -i <db container> pg_restore -U <user> -d orders --clean --if-exists --single-transaction
```

MySQL / MariaDB (SQL dump; the password is read from the container's environment):

```bash
restic dump <snapshot> /shop/db/legacy.sql \
  | docker exec -i <mysql container> sh -c 'exec mysql -u "$MYSQL_USER" -p"$MYSQL_PASSWORD" legacy'
```

Safety snapshots belong to no run: retention and `drkit prune` never remove them. Forget them
once they are no longer needed: `restic forget <snapshot>`.

## Restore a project

Use this when more than the data is wrong: the code, the compose files, a volume, or the whole
project directory. Everything comes from one run, so code, schema and files match.

1. **See the plan.**

   ```bash
   drkit restore project shop --dry-run
   ```

   Read the warnings. The usual ones: the checkout had **uncommitted changes** (tracked ones
   come back from the patch, untracked files were never backed up), an image **without a
   registry digest** (built locally; it will be pulled by tag or rebuilt), a volume of the
   manifest that **is not in the run**.

2. **Restore.**

   ```bash
   drkit restore project shop --run <run_id>
   ```

   The ten steps, each timed:

   | # | Step | Notes |
   |---|---|---|
   | 1 | Safety backup of the current project | What exists of it here (running databases, volumes, secrets, git state) is saved as `kind:pre-restore` snapshots tagged `restore:<restore_id>`. Nothing to save on a new host. |
   | 2 | Clone the repository at the recorded commit | The current directory is **moved** to `<path>.pre-restore-<restore_id>`, never deleted. The checkout is a detached HEAD at the recorded commit, plus the patch of uncommitted changes. |
   | 3 | Restore compose files and secrets | Secrets get mode `0600` and their original owner. |
   | 4 | Pull the images | By digest where one was recorded: exactly the images that ran. |
   | 5 | Start the database services | And wait until they accept connections. |
   | 6 | Restore the databases | Roles first, then each dump, then sanity checks. |
   | 7 | Restore the volumes | Services using a volume are stopped; its contents become identical to the backup. |
   | 8 | Start the project | Every service is recreated. |
   | 9 | Run `post_restore` hooks | E.g. migrations, from the project directory, with `DRKIT_PROJECT`, `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE` set (`docker compose exec …` reaches the project). |
   | 10 | Healthchecks | Retried with backoff until each check's `timeout`. |

   If the safety backup fails (a broken service, for example), the restore stops before
   changing anything. Fix it, or pass `--no-safety-backup` if the current state is worthless.
   To undo a project restore, restore the safety snapshots by hand like any other
   (`restic snapshots --tag restore:<restore_id>`; see [Undo a database restore](#undo-a-database-restore)).

3. **Check.** All healthchecks green ends the restore; then check the application as a user
   would. The report (per-step timings and the total duration, i.e. the RTO of this restore)
   is in `/var/lib/drkit/reports/`.

4. **Clean up.** Once the project works, keep or delete the moved-aside directory
   (`<path>.pre-restore-<restore_id>`); it may hold logs or files that were not in git. The
   checkout is on a detached HEAD: switch to the deploy branch before the next deploy
   (`git switch main`), if the deploy process expects one.

### When a step fails

drkit stops at the first failed step, prints why, and exits with code `5`:

```
error: restore stopped at step 7 (Restore the volumes): cannot write volume shop_uploads at /var/lib/docker/volumes/shop_uploads/_data
hint: fix the cause, then resume with --from-step 7 --run 20261029T030000Z-a1f3
```

Fix the cause and run the command from the hint: the steps before it are skipped. Steps can
be repeated, so resuming one step earlier than necessary is safe. Common causes:

| Step | Failure | Fix |
|---|---|---|
| 1 | `safety backup of … failed` | Fix the service it names, or restore with `--no-safety-backup`. |
| 2 | `cannot read git remote` / `git clone ... failed` | Deploy key missing or without read access; network. `drkit doctor` shows the git check. |
| 2 | `cannot check out commit` | The commit is gone from the remote (force-push). Restore an older run. |
| 3 | `outside the project directory` | `projects[].path` changed since the backup: set it back for this restore. |
| 3 | `declared external` | External volumes are not supported (DESIGN §5.2). |
| 5 | `database service ... did not start` | Read the database logs (`docker compose logs db`). |
| 7 | `cannot write volume` | drkit must run as root on the Docker host. |
| 9 | `post_restore hook failed` | Run the hook by hand from the project directory to see why. |
| 10 | `not healthy after ...` | Read the service logs; raise `timeout` if the service is only slow. |

### Restore to another directory

`--to-path /srv/shop-recovered` restores the project into another directory. The compose
project name stays the same, so it **replaces the running project**: use it when the original
path is not usable, not to run two copies side by side.

## Restore a host

Use this when the server is gone: a new machine gets the operating system configuration back
from Ansible, then the system configuration, every project, the certificates and nginx from
one run. Plan for the DNS change at the end.

### What you need, and where it is

Nothing on the lost server is needed. Before starting, have at hand:

| What | Where it should be |
|---|---|
| `dr.yaml`, `restic.pass` and `env` (`/etc/drkit/`) | Your password manager (or sealed envelope). **Without `restic.pass` and the storage credentials in `env`, the backups cannot be read.** `dr.yaml` holds no secret values. |
| The storage credentials (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `env`) | Same place; or create a new key for the same bucket in the storage provider's console. |
| Deploy keys (`projects[].git.deploy_key`) | Password manager; or add a new deploy key to each private repository. |
| Access to the DNS zone | Your DNS provider: the records must point to the new server at the end. |
| A fresh Ubuntu 22.04 or 24.04 server | Root SSH access. |

### Procedure

1. **Prepare the server** (installs Docker, restic, git, Ansible, certbot and drkit; safe to
   run again):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/caiodassumpcao/drkit/main/scripts/bootstrap.sh | sudo bash
   ```

2. **Put the keys back:**

   ```bash
   sudo install -d -m 700 /etc/drkit
   sudoedit /etc/drkit/dr.yaml /etc/drkit/restic.pass /etc/drkit/env   # paste from the vault
   sudo chmod 600 /etc/drkit/* && sudo chown root: /etc/drkit/*
   ```

   Restore deploy keys to the paths in `dr.yaml` the same way (mode `0600`).

3. **Check.** `sudo drkit doctor` must pass for the repository, the git remotes and the
   dependencies. Project checks that need running services fail at this point, as expected.

4. **See the plan**, and pick the run if `latest` is not the one:

   ```bash
   sudo drkit runs list
   sudo drkit restore host --dry-run [--run <run_id>]
   ```

5. **Restore:**

   ```bash
   sudo drkit restore host --run <run_id>
   ```

   | # | Step | Notes |
   |---|---|---|
   | 1 | Preflight | Root, release, dependencies, repository. Nothing changes yet. |
   | 2 | Provision the host with Ansible | The playbook of `system.ansible` at its `ref`, run locally. Full log in `/var/lib/drkit/reports/ansible-<restore_id>.log`. `--skip-system` skips it when the host is already provisioned. |
   | 3 | Restore system paths | Paths with `restore: true` (nginx sites, units, crontabs) go back where they were. |
   | 4 | Reload systemd and validate nginx | `nginx -t` may wait for the certificates of step 6. |
   | 5 | Restore the projects | Each project's ten steps, timed, in manifest order. |
   | 6 | TLS certificates | `restore`: from the backup. `reissue`: certbot, which needs the DNS already pointing here. |
   | 7 | Reload nginx and run all healthchecks | The end of the RTO. |
   | 8 | Recovery checklist and report | What is left to do, the package diff, the RTO. |

6. **Do the checklist** printed at the end (also in the report): point the DNS records to the
   new server, enable the drkit timers here (`systemctl enable --now drkit-backup.timer
   drkit-verify.timer drkit-prune.timer`), enable the restored units that should run, and
   review the packages installed by hand on the old server that are missing here (add those
   still needed to the playbook).

   **Make sure the old server never backs up again under the same `host`** if it comes back:
   its runs would mix with the new server's. Disable its timers or change its `host`.

### With `tls.strategy: reissue`

certbot validates each domain over HTTP, so the DNS records must point to the new server
before step 6. Either change the DNS before the restore (the sites are down anyway), or let
the restore stop at step 6, change the DNS, wait for it to propagate and resume with
`--from-step 6 --run <run_id>`.

### When a step fails

As for a project: fix the cause, then resume with the `--from-step` printed in the error. A
failure inside a project (step 5) names the `drkit restore project … --from-step` that resumes
that project; once it succeeds, resume the host restore with `--from-step 6`.

| Step | Failure | Fix |
|---|---|---|
| 1 | a dependency is missing | Run `bootstrap.sh` again. |
| 2 | `ansible-galaxy could not install` | Galaxy outage or no network: retry later, resume with `--from-step 2`. |
| 2 | `ansible-playbook failed` | Read the log named in the hint; fix the playbook or its `extra_vars`. |
| 3 | `is not in snapshot … of system.paths` | The path was added to the manifest after this backup: drop it for this restore or pick a newer run. |
| 4 | `nginx is configured … but not installed` | Install nginx from the playbook (drkit does not install packages). |
| 4 / 7 | `nginx -t failed` | Fix the configuration under `/etc/nginx`; it came from the backup. |
| 6 | `certbot could not issue` | DNS not pointing here yet, or port 80 closed. |
| 7 | `not healthy after …` | `docker compose logs`, `journalctl -u nginx`. |

### Rehearsing on CI

The CI job "Host recovery" runs this whole procedure on a disposable GitHub runner with the
[server-hardening-kit](https://github.com/caiodassumpcao/server-hardening-kit) playbook. A
runner must stay reachable, so the playbook runs with `skip_tags: [ssh, ufw]` and with
variables that keep the runner's own sudo (`users_purge_sudoers_files: []`), skip the
bootloader (`auditd_grub_config: false`) and keep IP forwarding for Docker
(`sysctl_hardening_ip_forward: true`). Use the same approach to rehearse on a scratch VM.
