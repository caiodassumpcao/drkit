# ADR-0003: Drive Docker through its CLI instead of the SDK

- **Status:** Accepted
- **Date:** 2026-10-28

## Context

drkit needs to resolve Compose services to containers, find volume mountpoints, stream
`docker exec` output (database dumps), stop/start services and run `docker compose
up/pull/down`. Python offers the `docker` SDK (Engine API), but Compose v2 is a Go CLI plugin
with no supported Python API.

## Decision

Drive Docker exclusively through the `docker` and `docker compose` CLIs, via `drkit.runner`,
using machine-readable output (`--format json`, `docker compose ps --format json`, labels such
as `com.docker.compose.project` and `com.docker.compose.volume`).

## Alternatives considered

| Option | Why not |
|---|---|
| **docker SDK for Python** | Covers the Engine API but not Compose; drkit would still shell out to `docker compose` and maintain two integration paths. Adds a dependency that must track the Engine API version. |
| **Re-implement Compose logic** (parse compose files, compute names/labels) | Compose semantics (profiles, overrides, interpolation, project naming) are large and change over time; any divergence breaks restores silently. |
| **Talk to the Engine API over the socket directly** | Same Compose gap, plus hand-written HTTP for streaming exec. |

## Consequences

- Same behavior operators get when they type the commands; failures can be reproduced by
  copying the (redacted) command from the logs.
- One uniform integration point: every Docker call gets the runner's timeouts, streaming pipes
  and secret redaction, and is easy to fake in unit tests.
- Process startup costs a few milliseconds per call: negligible next to dumps and uploads.
- drkit depends on CLI output formats; it only uses JSON/`--format` output and labels, never
  human-readable tables, and `drkit doctor` checks the minimum Docker and Compose versions.
