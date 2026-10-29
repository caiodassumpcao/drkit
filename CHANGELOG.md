# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/caiodassumpcao/drkit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/caiodassumpcao/drkit/releases/tag/v0.1.0
