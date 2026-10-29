# Architecture decision records

Each ADR records one significant decision: **Context · Decision · Alternatives considered ·
Consequences**. ADRs are immutable once accepted; a change of direction is a new ADR that
supersedes the old one.

| # | Title | Status |
|---|---|---|
| [0001](0001-use-restic-as-storage-engine.md) | Use restic as storage engine | Accepted |
| [0002](0002-rebuild-hosts-instead-of-imaging.md) | Recover hosts by rebuilding (IaC + data) instead of imaging | Accepted |
| [0003](0003-drive-docker-through-its-cli.md) | Drive Docker through its CLI instead of the SDK | Accepted |
| [0004](0004-run-manifest-as-unit-of-restore.md) | Run manifest as the unit of consistent restore | Accepted |
| [0005](0005-stream-database-dumps-via-stdin.md) | Stream database dumps to restic via stdin | Accepted |
