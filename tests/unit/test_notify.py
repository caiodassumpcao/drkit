import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from drkit import notify, runner
from drkit.config import Notifications
from tests.samples import TELEGRAM_TOKEN

TOKEN = TELEGRAM_TOKEN
WEBHOOK = "https://hooks.example.com/services/T0/B0/very-secret"


def config(
    on: list[str] | None = None, telegram: bool = True, webhook: bool = True
) -> Notifications:
    data: dict[str, object] = {}
    if on is not None:
        data["on"] = on
    if telegram:
        data["telegram"] = {"bot_token": TOKEN, "chat_id": "42"}
    if webhook:
        data["webhook"] = {"url": WEBHOOK}
    return Notifications.model_validate(data)


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._handler = handler or (lambda _: httpx.Response(200))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def send(self, notifications: Notifications | None, event: str, message: str) -> list[str]:
        client = httpx.Client(transport=httpx.MockTransport(self))
        return notify.notify(
            notifications,
            event,  # type: ignore[arg-type]
            message,
            client=client,
            sleep=self.sleeps.append,
        )


@pytest.fixture(autouse=True)
def _clean_redactor() -> Iterator[None]:
    runner.redactor.clear()
    yield
    runner.redactor.clear()


def test_sends_to_both_channels() -> None:
    recorder = Recorder()
    assert recorder.send(config(), "failure", "backup failed") == []

    telegram, webhook = recorder.requests
    assert str(telegram.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(telegram.content) == {
        "chat_id": "42",
        "text": "backup failed",
        "disable_web_page_preview": True,
    }
    assert str(webhook.url) == WEBHOOK
    assert json.loads(webhook.content) == {"text": "backup failed"}


def test_respects_subscribed_events() -> None:
    recorder = Recorder()
    assert recorder.send(config(), "success", "ok") == []  # default: failure only
    assert recorder.send(None, "failure", "x") == []
    assert recorder.requests == []

    recorder.send(config(on=["success"], telegram=False), "success", "ok")
    assert len(recorder.requests) == 1


def test_message_is_redacted_and_truncated() -> None:
    runner.redactor.add("db-password-1")
    recorder = Recorder()
    recorder.send(config(telegram=False), "failure", "auth db-password-1 " + "x" * 5000)
    text = json.loads(recorder.requests[0].content)["text"]
    assert "db-password-1" not in text
    assert len(text) == notify.MAX_MESSAGE_CHARS
    assert text.endswith("…")


def test_retries_server_errors_then_succeeds() -> None:
    responses = iter([httpx.Response(502), httpx.Response(200)])
    recorder = Recorder(lambda _: next(responses))
    assert recorder.send(config(telegram=False), "failure", "x") == []
    assert len(recorder.requests) == 2
    assert recorder.sleeps == [1.0]


def test_gives_up_after_retries() -> None:
    recorder = Recorder(lambda _: httpx.Response(503))
    assert recorder.send(config(telegram=False), "failure", "x") == ["webhook: HTTP 503"]
    assert len(recorder.requests) == 3
    assert recorder.sleeps == [1.0, 3.0]


def test_client_errors_are_not_retried() -> None:
    recorder = Recorder(lambda _: httpx.Response(401))
    assert recorder.send(config(webhook=False), "failure", "x") == ["telegram: HTTP 401"]
    assert len(recorder.requests) == 1


def test_rate_limit_is_retried() -> None:
    recorder = Recorder(lambda _: httpx.Response(429))
    recorder.send(config(webhook=False), "failure", "x")
    assert len(recorder.requests) == 3


def test_network_errors_never_leak_the_token() -> None:
    runner.redactor.add(TOKEN, WEBHOOK)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    errors = Recorder(fail).send(config(), "failure", "x")
    assert len(errors) == 2
    assert errors[0].startswith("telegram: ConnectError: cannot connect to")
    assert all(TOKEN not in e and "very-secret" not in e for e in errors)


def test_one_channel_failing_does_not_block_the_other() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if "telegram" in str(request.url) else 200)

    recorder = Recorder(handler)
    assert recorder.send(config(), "failure", "x") == ["telegram: HTTP 500"]
    assert str(recorder.requests[-1].url) == WEBHOOK


def test_creates_and_closes_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    clients: list[httpx.Client] = []
    real = httpx.Client

    def factory(**kwargs: object) -> httpx.Client:
        client = real(transport=httpx.MockTransport(recorder))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", factory)
    assert notify.notify(config(telegram=False), "failure", "x") == []
    assert clients[0].is_closed
