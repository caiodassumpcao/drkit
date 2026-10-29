# ADR-0001: Use restic as storage engine

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

drkit must store backups off-site, encrypted, deduplicated and verifiable, on any
S3-compatible storage. Writing a storage format means owning encryption, chunking,
deduplication, retention, integrity checking and repository locking: years of work, and the
part of a backup tool where a subtle bug costs the most. The value drkit adds is elsewhere:
knowing *what* a server needs to come back and *proving* that it does.

## Decision

Use [restic](https://restic.net/) (≥ 0.17) as the storage engine, driven as an external
binary through `drkit.runner`:

- every backed-up item becomes one restic snapshot, tagged with host, run, kind and item
  (DESIGN §5.3);
- database dumps are streamed with `restic backup --stdin` (see ADR-0005, upcoming);
- retention uses `restic forget`, integrity uses `restic check --read-data-subset`;
- drkit parses restic's `--json` output and never reads the repository format directly.

## Alternatives considered

| Option | Why not |
|---|---|
| **BorgBackup** | No native S3 backend (needs a Borg server over SSH or rclone mounts); a less natural fit for object storage. |
| **Kopia** | Technically comparable, but younger, with a smaller operational track record and a less stable CLI/JSON surface for automation. |
| **Plain `aws s3 cp` / `rclone` of archives** | No deduplication, so hourly backups multiply storage cost; encryption and integrity checks would have to be built. |
| **Own format** | Highest risk for the least differentiated part of the product. |
| **restic as a Go library** | Not a supported API, and it would force drkit out of Python. |

## Consequences

- Strong, audited client-side encryption (AES-256 + Poly1305) and content-defined
  deduplication for free; S3, B2, Azure, GCS, SFTP and local backends work unchanged.
- Operators can inspect and restore with plain `restic` if drkit itself is unavailable, which
  matters during a disaster.
- drkit depends on restic's CLI and JSON output staying stable; the minimum version is checked
  by `drkit doctor`, and integration tests run against a real restic binary.
- The restic password and storage credentials become the master recovery keys; their handling
  is covered in DESIGN §9 (and ADR-0006, upcoming).
