from collections.abc import Callable, Iterator

import httpx
import pytest

from drkit import health
from drkit.config import Healthcheck

URL = "http://127.0.0.1:8080/health"


class Clock:
    """Fake time: sleeping advances it, so backoff is observable without waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    fake = Clock()
    monkeypatch.setattr(health, "monotonic", fake.monotonic)
    monkeypatch.setattr(health, "sleep", fake.sleep)
    yield fake


def client(*answers: int | Exception) -> tuple[httpx.Client, list[httpx.Request]]:
    queue = list(answers)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def check(timeout: int = 60, expect_status: int = 200) -> Healthcheck:
    return Healthcheck.model_validate(
        {"url": URL, "timeout": timeout, "expect_status": expect_status}
    )


def test_healthy_at_once(clock: Clock) -> None:
    http, requests = client(200)
    result = health.wait_healthy(check(), client=http)
    assert result == health.HealthResult(URL, True, 1, 0.0, 200)
    assert clock.sleeps == []
    assert str(requests[0].url) == URL


def test_retries_with_exponential_backoff(clock: Clock) -> None:
    refused = httpx.ConnectError("connection refused")
    http, _ = client(refused, refused, 502, 502, 502, 200)
    result = health.wait_healthy(check(), client=http)
    assert result.ok
    assert result.attempts == 6
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0]
    assert result.elapsed_s == 25.0


def test_gives_up_at_the_deadline(clock: Clock) -> None:
    http, _ = client(503)
    result = health.wait_healthy(check(timeout=20), client=http)
    assert not result.ok
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 5.0]
    assert result.attempts == 6
    assert result.status_code == 503
    assert result.error == "not healthy after 20s (6 attempts): HTTP 503, expected 200"


def test_connection_errors_are_reported(clock: Clock) -> None:
    http, _ = client(httpx.ConnectError("connection refused"))
    result = health.wait_healthy(check(timeout=1), client=http)
    assert result.status_code is None
    assert result.error == "not healthy after 1s (2 attempts): ConnectError: connection refused"


def test_expected_status_other_than_200(clock: Clock) -> None:
    http, _ = client(200, 204)
    result = health.wait_healthy(check(expect_status=204), client=http)
    assert (result.ok, result.attempts) == (True, 2)


def test_own_client_is_closed(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    http, _ = client(200)
    monkeypatch.setattr(http, "close", lambda: closed.append(True))
    factory: Callable[[], httpx.Client] = lambda: http  # noqa: E731
    monkeypatch.setattr(httpx, "Client", factory)
    assert health.wait_healthy(check()).ok
    assert closed == [True]


def test_wait_all(clock: Clock) -> None:
    http, _ = client(200)
    results = health.wait_all([check(), check(expect_status=204)], client=http)
    assert [r.ok for r in results] == [True, False]
