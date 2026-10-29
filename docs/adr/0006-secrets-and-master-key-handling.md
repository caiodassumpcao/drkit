# ADR-0006: Secrets and master key handling

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

drkit handles two very different kinds of secrets:

- **The master key**: the restic password and the storage credentials. They open the
  repository, so they are what a recovery needs first and what an attacker would want most.
- **Application secrets**: `.env` files, private keys and database credentials of the
  projects being backed up. They are not in git (they must not be), so without a backup a
  rebuilt server has code but cannot run it.

The obvious move, "back everything up", fails for the master key: a copy stored inside the
repository can only be read by someone who already has it. And every place a secret passes
through (command lines, environment, logs, notifications, temporary files) is a place it can
leak from.

## Decision

**The master key lives outside the server and outside the repository.** It is three files in
`/etc/drkit/`: `restic.pass`, the env file with the storage credentials, and `dr.yaml`
(which holds no secret values itself, only `${VAR}` references). drkit never backs them up
and never prints them. The operator keeps a copy in a password manager (or a sealed
envelope); `drkit init` and the README say so prominently, and the restore runbook starts by
putting them back.

**drkit refuses to run with exposed key files.** The manifest, password file, env file and
deploy keys must be regular files owned by the user running drkit with no group/other
permission bits; `doctor` reports and explains any violation (DESIGN §9).

**Application secrets are a backup item of their own** (`kind:secrets`, one snapshot per
project of the files listed in `projects[].secrets`). They are encrypted client-side by
restic like every other item, so the storage provider never sees them, and they stay
separate from the code (cloned again from git) so a restore can write them back with
`0600` and their original owner.

**Secrets never travel where other users can see them:**

| Secret | How it is passed |
|---|---|
| restic password | `RESTIC_PASSWORD_FILE` (a path), never the value |
| Storage credentials | environment of the restic process only |
| MySQL password | read from the container environment, passed back through `docker exec -e MYSQL_PWD` with the value in drkit's environment, never on argv (readable by any local user through `/proc`) |
| PostgreSQL | local socket authentication inside the container: no password at all |
| Database users and passwords | read from the container at backup time, never copied into the manifest |

**Everything printed is redacted.** Values from the env file, secret manifest fields
(notification tokens and URLs) and values passed to commands through `env=` are registered
with the redactor, which also masks well-known patterns (`*PASSWORD*=`, `*TOKEN*=`,
credentials in URLs, Telegram bot tokens) in logs, error messages, reports and
notifications.

**Copies of data stay contained.** `drkit verify` restores production dumps into throwaway
containers started with `--network none` and always removed afterwards.

## Alternatives considered

| Option | Why not |
|---|---|
| **Store the master key in the repository** | Circular: it is needed to open the repository. |
| **`RESTIC_PASSWORD` environment variable** | The value is inherited by every child process and ends up in crash reports and debug dumps; a file path is inert. |
| **Separate repository (and key) for application secrets** | One more key to lose and rotate for little gain: restic already encrypts every snapshot with the repository key. |
| **Require encrypted secrets in git (sops/age)** | A good practice, but a project decision drkit cannot impose; drkit backs up what the server needs to run, whatever its origin. |
| **Cloud KMS / Vault for the master key** | Adds a runtime dependency that must itself be recovered first; a password-manager copy has no such dependency. |

## Consequences

- Losing `restic.pass` loses every backup, permanently; this is stated wherever the key is
  created or used. Rotation is done with `restic key add` / `restic key remove` (documented,
  not automated).
- Anyone holding the restic password **and** the storage credentials can read every
  application secret. The storage key should be scoped to one bucket or prefix, and bucket
  versioning or object lock protects the backups against a compromised server deleting them.
- Root on the protected server can read the master key; drkit does not try to defend
  against its own host's root.
- The secrets snapshot can be inspected with `restic dump`, which makes audits and partial
  recoveries possible without drkit.
