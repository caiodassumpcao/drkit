# ADR-0004: Run manifest as the unit of consistent restore

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

A backup run produces many independent restic snapshots: one per database dump, and from
v0.2 one per volume, secrets set, project state and system path set. Restoring a project
needs a set of snapshots that belong together: the database dump that matches the uploaded
files, the code at the commit that matches the schema. restic itself has no notion of
"these snapshots form one backup"; picking the latest snapshot of each item independently
can mix runs, especially when an item failed in the latest run.

## Decision

Every run writes a **run manifest**, a JSON document (DESIGN §5.4), as its last snapshot:

- stored with `restic backup --stdin` as `runs/<run_id>.json`, tagged `drkit`,
  `host:<host>`, `run:<run_id>`, `kind:meta` and `status:<ok|partial|failed>`;
- it lists every item with its status, snapshot ID(s), size, duration and engine version
  (and from v0.2 the git SHA and image digests of each project);
- it is written even when items failed (`partial`), so failures are part of the record;
- `run_id` is `YYYYMMDDTHHMMSSZ-<4 hex>`: sortable and unique for runs in the same second.

**Restores always start from a run manifest** (`--run latest|<id>`), never from a free
search for "the latest snapshot of X".

**Retention operates on runs.** The keep policy (`daily`/`weekly`/`monthly`) is evaluated
over the `kind:meta` snapshots of the host, grouped by host only; for every run it discards,
all snapshots tagged `run:<id>` are forgotten. A kept run is therefore always complete.

## Alternatives considered

| Option | Why not |
|---|---|
| **Latest snapshot per item** | Mixes runs; after a failed item the restore silently combines an old dump with new files. |
| **One snapshot per run with every item inside** | Database dumps are streams (ADR-0005) and volumes are directories; one snapshot would need temp files or a staging tree, and a failed item would invalidate the whole snapshot. |
| **Run state only in local `state.json`** | Lost with the server, which is exactly when it is needed. |
| **`restic forget --group-by host,tags`** (first draft of DESIGN §5.1) | Every snapshot carries a unique `run:<id>` tag, so each would form its own group and nothing would ever be forgotten. Grouping per item (`host,paths`) works, but can keep a manifest whose items were forgotten. |

## Consequences

- One manifest download tells a restore exactly what to fetch; `runs list` only needs
  snapshot tags (`status:` tag), not a download per run.
- A run without a manifest (crash before step 7) cannot be restored as a set; its orphan
  snapshots are not referenced by any manifest and are not removed by run retention.
  `drkit prune` (v0.2) forgets them: under the run lock, snapshots whose `run:<id>` has no
  `kind:meta` snapshot and that are **older than 24 hours** are forgotten before
  `restic prune`. The lock already rules out a run in progress on this machine; the age
  limit also protects a run in progress elsewhere under the same host name (e.g. on a
  server being recovered) and absorbs clock skew. Snapshots without a `run:` tag are never
  touched.
- Retention keeps the newest run of each period even if it is `partial`. The previous
  complete run of that period is dropped, as restic would do per snapshot.
- The manifest schema is versioned (`"schema": 1`) and readers reject unknown fields, so a
  format change is always explicit. Until v1.0, schema 1 may gain optional fields
  (v0.2: `projects.<name>.patch_snapshot`); a reader only ever reads manifests written by
  the same or an older drkit, since the binary on the host writes them.
