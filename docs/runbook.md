# Recovery runbook

Step-by-step procedures for bringing data and services back with drkit. Each procedure says
what you need, what to run, how to check the result and how to undo it. Commands assume a
root shell on the server (`sudo -i`).

| Situation | Procedure |
|---|---|
| A table was dropped, a migration went wrong, rows were deleted | [Restore a database](#restore-a-database) |
| A bad deploy, a corrupted volume, a deleted project directory | [Restore a project](#restore-a-project) |
| The server is gone | Restore a host (v0.4) |

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

   The nine steps, each timed:

   | # | Step | Notes |
   |---|---|---|
   | 1 | Clone the repository at the recorded commit | The current directory is **moved** to `<path>.pre-restore-<restore_id>`, never deleted. The checkout is a detached HEAD at the recorded commit, plus the patch of uncommitted changes. |
   | 2 | Restore compose files and secrets | Secrets get mode `0600` and their original owner. |
   | 3 | Pull the images | By digest where one was recorded: exactly the images that ran. |
   | 4 | Start the database services | And wait until they accept connections. |
   | 5 | Restore the databases | Roles first, then each dump, then sanity checks. |
   | 6 | Restore the volumes | Services using a volume are stopped; its contents become identical to the backup. |
   | 7 | Start the project | Every service is recreated. |
   | 8 | Run `post_restore` hooks | E.g. migrations, from the project directory. |
   | 9 | Healthchecks | Retried with backoff until each check's `timeout`. |

   Restoring a project replaces its databases and volumes **without** a safety dump. If the
   current data might still be needed, run `drkit backup --project shop` first.

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
error: restore stopped at step 6 (Restore the volumes): cannot write volume shop_uploads at /var/lib/docker/volumes/shop_uploads/_data
hint: fix the cause, then resume with --from-step 6 --run 20261029T030000Z-a1f3
```

Fix the cause and run the command from the hint: the steps before it are skipped. Steps can
be repeated, so resuming one step earlier than necessary is safe. Common causes:

| Step | Failure | Fix |
|---|---|---|
| 1 | `cannot read git remote` / `git clone ... failed` | Deploy key missing or without read access; network. `drkit doctor` shows the git check. |
| 1 | `cannot check out commit` | The commit is gone from the remote (force-push). Restore an older run. |
| 2 | `outside the project directory` | `projects[].path` changed since the backup: set it back for this restore. |
| 2 | `declared external` | External volumes are not supported (DESIGN §5.2). |
| 4 | `database service ... did not start` | Read the database logs (`docker compose logs db`). |
| 6 | `cannot write volume` | drkit must run as root on the Docker host. |
| 8 | `post_restore hook failed` | Run the hook by hand from the project directory to see why. |
| 9 | `not healthy after ...` | Read the service logs; raise `timeout` if the service is only slow. |

### Restore to another directory

`--to-path /srv/shop-recovered` restores the project into another directory. The compose
project name stays the same, so it **replaces the running project**: use it when the original
path is not usable, not to run two copies side by side.

## Restore a host

Recovery of a lost server (Ansible, system paths, every project, TLS) arrives in v0.4 with
`drkit restore host`.
