# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-29

Everything a server needs besides its databases, and proof that the database backups restore.

### Added

- **Volumes.** Compose volumes are backed up from their mountpoint (incremental). With
  `consistency: stop`, the running containers that mount the volume are stopped for the backup
  and always started again, even when the backup fails.
- **Secrets.** The files in `projects[].secrets` (e.g. `.env`) are a backup item of their own,
  encrypted like everything else (ADR-0006).
- **Project state.** Each run records the git remote and commit, a patch of uncommitted
  changes (`patch_snapshot`), the compose files, and every running service's image pinned by
  registry digest.
- **System.** `system.paths` (with `include` globs) and an inventory of the host: OS release,
  kernel, Docker version and installed packages.
- **Hooks.** `pre_backup` / `post_backup` run around each project's items; `post_backup` always
  runs, and a failed `pre_backup` skips that project's items.
- **`drkit verify`.** `restic check` on part of the data (`--read-data-subset`, default 5%),
  then every database of the run is restored into a throwaway container of the same engine
  and version line, with no network, and checked with sanity queries and the optional
  `verify_query`. The result goes to `state.json` and to notifications.
- **`drkit prune`.** Forgets the snapshots of runs that never stored their run manifest and
  are older than 24 hours, then runs `restic prune`.
- **`drkit report`.** Recent runs, current RPO and last verification, as Markdown or JSON.
- `drkit doctor` checks each project's directory, database services and volumes.
- `drkit backup --only` accepts `db`, `volume`, `secrets`, `project` and `system`.
- Daily verify and weekly prune systemd timers in `examples/systemd/`.

### Changed

- MySQL credentials fall back to `MARIADB_USER` / `MARIADB_PASSWORD` when the `MYSQL_*`
  variables are not set, so official MariaDB images work without `user_env` /
  `password_env`. An explicit setting is still used alone.
- Path backups are all-or-nothing: when restic cannot read every file, the snapshot it saved
  anyway is forgotten and the item fails.
- GitHub Actions are pinned by commit SHA and integration test images by digest; Dependabot
  keeps both up to date. Integration tests run as root in CI, like drkit in production.

## [0.1.0] - 2026-10-29

First release: database backups to any S3-compatible storage, with retention and alerts.

### Added

- **Database backups.** PostgreSQL (`pg_dump -Fc` plus roles from `pg_dumpall --globals-only`)
  and MySQL / MariaDB (`mysqldump` or `mariadb-dump`), run inside the database containers and
  streamed straight into restic, with no temporary files (ADR-0005). A dump that fails never
  leaves a snapshot behind: if restic stored a truncated stream, that snapshot is forgotten at
  once and the item is reported as failed.
- **Runs and run manifests.** Each `drkit backup` is a run with an ID such as
  `20261029T030000Z-a1f3`. Its manifest (items, snapshot IDs, sizes, durations, engine
  versions, errors) is stored in the repository as the last snapshot of the run (ADR-0004).
  All snapshots are tagged `drkit`, `host:`, `run:`, `kind:`, `project:` and `db:`.
- **Retention per run.** The `daily` / `weekly` / `monthly` policy is applied to whole runs
  after every backup, so a kept run is always complete.
- **Alerts.** Telegram and Slack-compatible webhooks, on `failure` and/or `success`, with
  retries and secret redaction.
- **Commands.** `drkit init` (also creates the bucket), `drkit backup [--project] [--only db]`,
  `drkit runs list`, `drkit runs show [latest|<run_id>]`, all with `--json`.
- `drkit doctor` checks that the repository is reachable and the password is accepted.
- `databases[].password_env` in the manifest: the container variable holding the MySQL
  password (default `MYSQL_PASSWORD`). Credentials are read from the container environment and
  never appear on a command line.
- `state.json` in `/var/lib/drkit` with the last run and the last successful run.
- `DRKIT_STATE_DIR` and `DRKIT_LOCK_FILE` to run without root (tests, development).
- Example systemd service and hourly timer with hardening, in `examples/systemd/`.
- Integration tests against real MinIO, PostgreSQL, MySQL and MariaDB, also run in CI.

### Foundation

- Manifest (`dr.yaml`) models with strict validation, `${VAR}` interpolation from the
  environment and `repository.env_file`, precise error messages that never echo values, and
  owner-only permission checks for the manifest, password file and env file.
- Subprocess runner with mandatory timeouts, streaming pipes that report both exit codes, and
  secret redaction; structured logging (JSON off a TTY) with redaction; host-wide run lock;
  error hierarchy mapped to documented exit codes.
- `drkit doctor`: manifest, file permissions and dependencies (restic, Docker, Compose, git,
  and Ansible when configured), with fix hints.
- Annotated example manifest, CI (lint, types, unit tests, build, Gitleaks), Dependabot,
  issue and pull request templates, `SECURITY.md`, `CONTRIBUTING.md`, ADRs 0001–0003.

[Unreleased]: https://github.com/caiodassumpcao/drkit/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/caiodassumpcao/drkit/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/caiodassumpcao/drkit/releases/tag/v0.1.0
