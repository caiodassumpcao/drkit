"""HTTP healthchecks: retried with exponential backoff until the check's timeout runs out.

A restored service takes a while to answer (migrations, caches warming up), so a single
request proves nothing either way; a deadline with backoff waits as long as configured
without hammering a service that is still starting.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass

import httpx
import structlog

from drkit import runner
from drkit.config import Healthcheck

INITIAL_DELAY_S = 1.0
MAX_DELAY_S = 10.0
REQUEST_TIMEOUT_S = 10.0

log = structlog.get_logger(__name__)
sleep = time.sleep  # replaced in tests
monotonic = time.monotonic  # replaced in tests


@dataclass(frozen=True, slots=True)
class HealthResult:
    url: str
    ok: bool
    attempts: int
    elapsed_s: float
    status_code: int | None = None
    error: str | None = None


def wait_healthy(check: Healthcheck, *, client: httpx.Client | None = None) -> HealthResult:
    """GET ``check.url`` until it answers ``check.expect_status`` or ``check.timeout`` passes."""
    url = str(check.url)
    started = monotonic()
    deadline = started + check.timeout
    delay = INITIAL_DELAY_S
    attempts = 0
    status_code: int | None = None
    error = "no attempt"
    http = client or httpx.Client()
    try:
        while True:
            attempts += 1
            remaining = max(deadline - monotonic(), 1.0)
            try:
                response = http.get(url, timeout=min(REQUEST_TIMEOUT_S, remaining))
            except httpx.HTTPError as exc:
                status_code, error = None, f"{type(exc).__name__}: {exc}"
            else:
                status_code = response.status_code
                if status_code == check.expect_status:
                    elapsed = round(monotonic() - started, 1)
                    log.info("health.ok", url=url, attempts=attempts, elapsed_s=elapsed)
                    return HealthResult(url, True, attempts, elapsed, status_code)
                error = f"HTTP {status_code}, expected {check.expect_status}"
            now = monotonic()
            if now >= deadline:
                break
            log.debug("health.retry", url=url, attempt=attempts, error=error)
            sleep(min(delay, deadline - now))
            delay = min(delay * 2, MAX_DELAY_S)
    finally:
        if client is None:
            http.close()
    elapsed = round(monotonic() - started, 1)
    message = runner.redact(f"not healthy after {check.timeout}s ({attempts} attempts): {error}")
    log.error("health.failed", url=url, error=message)
    return HealthResult(url, False, attempts, elapsed, status_code, message)


def wait_all(
    checks: Iterable[Healthcheck], *, client: httpx.Client | None = None
) -> list[HealthResult]:
    """Run every check (each with its own deadline), so one report shows them all."""
    return [wait_healthy(check, client=client) for check in checks]
