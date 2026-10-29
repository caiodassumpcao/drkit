# ADR-0002: Recover hosts by rebuilding (IaC + data) instead of imaging

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

Whole-host recovery can either *clone* the lost machine (disk image, provider snapshot) or
*rebuild* it: provision a fresh OS from code, then restore only the data that cannot be
regenerated. drkit targets Ubuntu servers running Docker Compose projects whose system
configuration can be expressed as an Ansible playbook (for example `server-hardening-kit`).

## Decision

`drkit restore host` rebuilds:

1. a fresh, supported Ubuntu server is bootstrapped (`scripts/bootstrap.sh`);
2. system configuration is applied from Infrastructure as Code (`system.ansible`, run locally);
3. a small, explicit set of system paths is restored from backup (`system.paths` with
   `restore: true`, such as Nginx sites, units and crontabs);
4. projects are restored from data: code at the recorded git SHA, secrets, databases,
   volumes, then `docker compose up` and healthchecks.

Package inventory is backed up for information only (to diff before/after), never replayed.

## Alternatives considered

| Option | Why not |
|---|---|
| **Disk images / provider snapshots** | Tied to one provider and disk layout; images capture drift, malware and broken state along with data; large and slow to move off-site; no fine-grained restore. Still a good *complement*, documented as a non-goal. |
| **Back up all of `/` with restic** | Restoring a full root filesystem over a running different kernel/package set is fragile and hard to test; it inherits all the drift of the old host. |
| **[ReaR](https://relax-and-recover.org/)** | Excellent for bare-metal recovery of hand-configured machines, but recovers the old state as-is and does not understand Compose projects or databases. |

## Consequences

- Recovery works on any provider and on a newer Ubuntu release, and yields a clean,
  hardened host instead of a copy of a possibly compromised one.
- Recovery is testable end to end on a disposable machine, which is what the DR drill does in
  CI (DESIGN §8).
- Hand-made changes outside the playbook and outside `system.paths` are **not** recovered:
  servers must be managed as code. The runbook makes this explicit, and the inventory diff in the restore report
  surfaces packages that were never codified.
- RTO includes provisioning time (Ansible run); it is measured and reported rather than
  assumed.
