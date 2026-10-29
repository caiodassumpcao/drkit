# ADR-0005: Stream database dumps to restic via stdin

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

Database dumps can be as large as the database. Writing them to a temporary file first
doubles the disk I/O, needs free space equal to the dump size (often not available on the
server being protected), and leaves plaintext copies of the data on disk if the process
dies. But a pipe has a well-known trap: `pg_dump | restic backup --stdin` succeeds from
restic's point of view even when `pg_dump` fails halfway, and the resulting snapshot holds a
truncated dump that looks like a valid backup.

## Decision

Dumps run inside the database container (`docker exec`) and are streamed straight into
`restic backup --stdin --stdin-filename <project>/db/<db>.<ext>` through
`drkit.runner.pipe`, which waits for **both** processes and reports both exit codes:

| Dump | restic | Outcome |
|---|---|---|
| ok | ok | item `ok`, snapshot recorded in the run manifest |
| failed | ok (snapshot created) | snapshot **forgotten immediately**, item `failed` with the dump's stderr |
| failed | failed | item `failed` with the dump's error |
| ok | failed | item `failed` with restic's error |

restic's own exit code is not enough to tell whether a snapshot exists: when the dump dies
before writing anything, restic exits with 3 ("source could not be read") and still saves an
empty snapshot. drkit therefore reads the snapshot ID from restic's JSON summary whatever the
exit code, and forgets it unless both sides succeeded.

PostgreSQL also streams `pg_dumpall --globals-only` (roles) as a second snapshot of the same
item; if it fails, the dump snapshot is forgotten too, so an item is either complete or
absent. Credentials are read from the container environment; the MySQL password is passed
through `docker exec -e MYSQL_PWD` from drkit's environment, never on a command line.

## Alternatives considered

| Option | Why not |
|---|---|
| **Temporary file, then `restic backup <file>`** | Needs free disk equal to the dump, doubles I/O, leaves plaintext data on disk on crash. |
| **`restic backup --stdin-from-command`** (restic ≥ 0.17) | restic checks the command's exit code itself and skips the snapshot on failure, which is attractive. But restic then owns the dump process: drkit loses its stderr (needed for an actionable error), its separate timing, and the single runner path that applies timeouts and redaction to every command. Worth revisiting if those limits go away. |
| **`set -o pipefail` in a shell pipeline** | Requires `shell=True`, which drkit forbids outside manifest hooks; also loses which side failed. |
| **Dump to a volume and back up the directory** | Same disk cost as a temp file, plus a second copy inside Docker storage. |

## Consequences

- Constant memory and no temporary files, whatever the database size.
- A failed dump leaves no snapshot behind; covered by an integration test against a real
  PostgreSQL and MinIO.
- The forgotten snapshot's data is reclaimed by the next `drkit prune`, like any forget.
- The dump tools come from the database image itself, so client and server versions always
  match.
