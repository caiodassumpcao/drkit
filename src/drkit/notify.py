"""Alerts to Telegram and Slack-compatible webhooks.

Delivery is best effort: a notification failure is logged and reported, but never changes the
outcome of the backup it describes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import structlog

from drkit.config import Notifications, NotifyEvent
from drkit.runner import redact

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_S = 10.0
# Delays before each retry; network blips and 5xx are retried, 4xx are not (they won't fix
# themselves: wrong token, wrong chat ID, revoked webhook).
RETRY_DELAYS_S: Sequence[float] = (1.0, 3.0)
# Telegram rejects messages above 4096 characters.
MAX_MESSAGE_CHARS = 4000

log = structlog.get_logger(__name__)


def notify(
    notifications: Notifications | None,
    event: NotifyEvent,
    message: str,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Send ``message`` to every configured channel subscribed to ``event``.

    Returns one (redacted) error string per channel that could not be reached.
    """
    if notifications is None or event not in notifications.on:
        return []
    text = redact(message)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"

    deliveries: list[tuple[str, str, dict[str, Any]]] = []
    if notifications.telegram is not None:
        token = notifications.telegram.bot_token.get_secret_value()
        deliveries.append(
            (
                "telegram",
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                {
                    "chat_id": notifications.telegram.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        )
    if notifications.webhook is not None:
        deliveries.append(("webhook", notifications.webhook.url.get_secret_value(), {"text": text}))

    errors: list[str] = []
    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        for channel, url, payload in deliveries:
            error = _post(http, url, payload, sleep)
            if error is None:
                log.info("notify.sent", channel=channel, notify_event=event)
            else:
                log.warning("notify.failed", channel=channel, error=error)
                errors.append(f"{channel}: {error}")
    finally:
        if owned:
            http.close()
    return errors


def _post(
    client: httpx.Client, url: str, payload: dict[str, Any], sleep: Callable[[float], None]
) -> str | None:
    error = "not attempted"
    for attempt, delay in enumerate((0.0, *RETRY_DELAYS_S)):
        if attempt:
            sleep(delay)
        try:
            response = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # The exception text may embed the URL, which holds the token: redact it.
            error = redact(f"{type(exc).__name__}: {exc}")
            continue
        if response.is_success:
            return None
        error = f"HTTP {response.status_code}"
        if response.status_code < 500 and response.status_code != 429:
            break
    return error
