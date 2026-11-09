# Configuration reference (`dr.yaml`)

Everything drkit knows about a server is in one manifest. This page describes every key;
[`examples/dr.yaml`](../examples/dr.yaml) is an annotated example, and
[DESIGN.md](DESIGN.md) explains how each part is used.

## The file

| | |
|---|---|
| Location | `/etc/drkit/dr.yaml`; `--config <path>` or `$DRKIT_CONFIG` to use another file |
| Owner and mode | A regular file owned by the user running drkit (root in production), mode `0600`: drkit refuses to run otherwise. `drkit doctor` checks it, the password file, the env file and the deploy keys. |
| Format | YAML 1.2 booleans (`true`/`false` only: `on`, `yes`, `no` are strings). Duplicate keys are errors. Unknown keys are errors. |
| Errors | All problems are reported at once, with the file and the key path (`projects[0].databases[1].engine: …`). Values are never echoed: the file holds secrets. |
| Versioning | `version: 1` is the only schema. It is covered by the [compatibility policy](../README.md#compatibility): new optional keys may appear in 1.x, nothing changes meaning or goes away before 2.0. |

### Variables

`${NAME}` is replaced with the value of `NAME` from `repository.env_file` or from the
environment (the environment wins). An undefined variable is an error naming it and where it
is used. Only the braced form is interpolated: `$NAME` is left as is (shell variables in
hooks), and `$$` writes a literal `$`. `repository.env_file` itself may only use variables
of the environment.

The env file holds `KEY=VALUE` lines: `#` comments, an optional `export ` prefix, values
optionally quoted, no inline comments. Its values, and the secret keys of the manifest
(notification tokens and URLs), are redacted from every log line, error, report and
notification.

## Top level

| Key | Type | Required | Description |
|---|---|---|---|
| `version` | `1` | yes | Schema version. |
| `host` | name | yes | Logical name of the server: snapshots, runs and retention belong to it, whatever the machine's hostname. A recovered server keeps the same `host`; two servers must never share one. 2–41 characters: lowercase letters, digits, `-`, starting with a letter or digit. |
| `repository` | mapping | yes | [Where backups go](#repository). |
| `retention` | mapping | yes | [How many runs are kept](#retention). |
| `system` | mapping | no | [Host configuration](#system): Ansible, system paths, TLS. |
| `projects` | list | no | [The Compose projects](#projects) of the server. Names are unique. |
| `notifications` | mapping | no | [Alerts](#notifications). |
| `metrics` | mapping | no | [Prometheus metrics file](#metrics) (since 1.1). |

## `repository`

| Key | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | restic repository, usually `s3:https://<endpoint>/<bucket>/<prefix>` (any S3-compatible storage; other restic backends work too). Credentials in a URL are redacted from output. |
| `password_file` | absolute path | yes | The restic password (the repository key), mode `0600`. **Keep a copy outside the server**: without it the backups cannot be read. |
| `env_file` | absolute path | no | `KEY=VALUE` variables for restic and for `${…}` in the manifest: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `DRKIT_*` secrets. Mode `0600`. |

## `retention`

At least one of the three. Applied to whole runs after every backup (ADR-0004): a kept run is
always complete.

| Key | Type | Description |
|---|---|---|
| `daily` | positive integer | Keep the newest run of each of the last N days that have runs. |
| `weekly` | positive integer | … of each of the last N weeks. |
| `monthly` | positive integer | … of each of the last N months. |

## `system`

### `system.ansible`

Rebuilds the operating system during `drkit restore host` (step 2). Optional.

| Key | Type | Default | Description |
|---|---|---|---|
| `source` | string | — | Git URL or local path of the playbook repository. A local directory that is not a git repository is used as is. |
| `ref` | string | default branch | Tag, branch or commit to check out. Pin a tag. |
| `playbook` | relative path | `site.yml` | Playbook inside the repository. |
| `extra_vars` | mapping | `{}` | Passed with `-e` (through a private file, never on the command line). drkit runs `ansible-playbook -i localhost, -c local`, so a playbook targeting an inventory group needs a variable pointing it at `localhost` (e.g. `hardening_target: localhost` for server-hardening-kit). |
| `skip_tags` | list of strings | `[]` | Passed with `--skip-tags`, e.g. `[ssh, ufw]` where the run must not touch SSH access. |

`requirements.yml` at the repository root is installed with `ansible-galaxy install -r`.

### `system.paths`

Host configuration backed up at every run (`kind:system`, item `paths`): nginx sites, systemd
units, crontabs.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | absolute path | — | A file or directory. Missing paths fail the item. |
| `include` | list of globs | `[]` (all) | Only the files under `path` matching these patterns, e.g. `["app-*.service"]`. |
| `restore` | boolean | `false` | Restored to its location by `restore host` (step 3), without deleting files the playbook created. Units restored this way are enabled again if they were enabled on the old server. |

### `system.tls`

How `restore host` brings back the certificates nginx uses (step 6). Optional.

| Key | Type | Description |
|---|---|---|
| `strategy` | `reissue` or `restore` | `reissue`: certbot issues each certificate referenced by the enabled nginx configuration again, for the `server_name` of the blocks that use it (the DNS must point to the new server; certbot runs in standalone mode with nginx stopped). `restore`: `/etc/letsencrypt` comes back from the backup, so it must be in `system.paths`. |
| `email` | email | Required with `reissue`: the ACME account address. |

## `projects`

One entry per Docker Compose project.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | name | — | Project name in drkit: tags, labels, commands (`drkit restore project <name>`). Same rules as `host`. |
| `path` | absolute path | — | The project's git checkout on the server. |
| `git` | mapping | — | [Code](#projectsgit). |
| `compose` | mapping | — | [Compose files](#projectscompose). |
| `secrets` | list of relative paths | `[]` | Files next to the code that are not in git (`.env`, keys), backed up as their own item and restored with mode `0600` and their original owner. Paths cannot leave the project directory (`..`). |
| `databases` | list | `[]` | [Databases](#projectsdatabases) to dump. |
| `volumes` | list | `[]` | [Docker volumes](#projectsvolumes) to back up. |
| `hooks` | mapping | — | [Commands around backups and restores](#projectshooks). |
| `healthchecks` | list | `[]` | [HTTP checks](#projectshealthchecks) that end a restore. |

### `projects[].git`

| Key | Type | Description |
|---|---|---|
| `remote` | string | Remote the code is cloned from when restoring (SSH, HTTPS or a local path). The commit, the remote and a patch of uncommitted changes are recorded at every run. |
| `deploy_key` | absolute path | Optional SSH key for a private repository (mode `0600`), used through `core.sshCommand`. `doctor` proves the remote is readable with it. |

### `projects[].compose`

| Key | Type | Default | Description |
|---|---|---|---|
| `project_name` | string | `name` | Compose project name (`-p`): containers and volumes are found by its labels. |
| `files` | list of relative paths | — | Compose files, at least one, backed up at every run and used to restore. |

### `projects[].databases`

Dumped inside their containers and streamed into restic, with no temporary file (ADR-0005).
Supported: PostgreSQL ≥ 14, MySQL ≥ 8.4, MariaDB ≥ 10.11.

| Key | Type | Default | Description |
|---|---|---|---|
| `engine` | `postgres` or `mysql` | — | `mysql` covers MySQL and MariaDB. |
| `service` | string | — | Compose service running the database. |
| `database` | string | — | Database name (`[A-Za-z0-9_-]`, up to 63 characters), unique within the project. |
| `user_env` | variable name | see below | Variable **in the container** holding the user. Default: `POSTGRES_USER`; `MYSQL_USER`, then `MARIADB_USER`. |
| `password_env` | variable name | see below | `mysql` only. Variable in the container holding the password. Default: `MYSQL_PASSWORD`, then `MARIADB_PASSWORD`. |
| `verify_query` | string | — | Extra SQL run by `drkit verify` and the restore sanity check, e.g. `SELECT count(*) FROM users`. |

Credentials are read from the container, never copied into the manifest. PostgreSQL dumps
use the container's local socket and need no password.

### `projects[].volumes`

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | — | Volume name in the compose file (resolved through Compose's labels). External volumes are not supported. |
| `consistency` | `live` or `stop` | `live` | `stop` stops the running containers that mount the volume during its backup and always starts them again. |

### `projects[].hooks`

Shell commands (`sh -c`, as root, 30 minutes each), run from the project directory with
`DRKIT_PROJECT`, `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE` set, so `docker compose …` reaches
the project. They come from the manifest, which only its owner can write.

| Key | Description |
|---|---|
| `pre_backup` | Before the project's items. A failure skips them. |
| `post_backup` | After them, always (also after failures). |
| `post_restore` | Step 9 of `restore project` (e.g. migrations). A failure stops the restore. |

### `projects[].healthchecks`

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | http(s) URL | — | `GET` target. |
| `expect_status` | integer | `200` | Status that counts as healthy. |
| `timeout` | positive integer | `60` | Seconds to keep trying, with exponential backoff (1 s, doubling up to 10 s). |

## `notifications`

At least one channel. Delivery failures are reported as warnings and never change an exit
code.

| Key | Type | Default | Description |
|---|---|---|---|
| `on` | list of `failure`, `success` | `[failure]` | Which outcomes of backup, verify and prune are sent. Restores are always announced, success or failure. |
| `telegram.bot_token` | string | — | Bot token (use `${DRKIT_TG_TOKEN}`). |
| `telegram.chat_id` | string | — | Chat to post to. |
| `webhook.url` | http(s) URL | — | Receives `{"text": …}` (Slack-compatible). Use `${DRKIT_WEBHOOK_URL}`. |

## `metrics`

Since 1.1. Optional: without it, drkit writes no metrics file (`drkit metrics` still prints
them). See [monitoring.md](monitoring.md).

| Key | Type | Description |
|---|---|---|
| `textfile` | absolute path ending in `.prom` | File in node_exporter's `--collector.textfile.directory`, rewritten atomically (mode `0644`) after every backup, verify, prune, restore and drill. Its directory must exist and be writable (`doctor` checks it); a failed write is only a warning. |

```yaml
metrics:
  textfile: /var/lib/node_exporter/textfile_collector/drkit.prom
```

## Files and directories drkit uses

| Path | Override | Content |
|---|---|---|
| `/etc/drkit/dr.yaml` | `--config`, `$DRKIT_CONFIG` | The manifest |
| `/var/lib/drkit/state.json` | `$DRKIT_STATE_DIR` | Last run, verify and drill |
| `/var/lib/drkit/reports/` | `$DRKIT_STATE_DIR` | Restore and drill reports, Ansible logs |
| `metrics.textfile` | — | Prometheus metrics, if configured |
| `/run/drkit.lock` | `$DRKIT_LOCK_FILE` | Run lock (backup, prune, restores) |
