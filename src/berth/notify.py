"""Post the drift report to Slack through an incoming webhook. Not yet run against a real workspace."""

import json
import urllib.request

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_TIMEOUT_SECONDS = 30


class SlackSettings(BaseSettings):
    """SLACK_WEBHOOK_URL, from a Key Vault-linked variable group. Required: a report nobody receives is a silent failure."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)
    slack_webhook_url: SecretStr


class NotifyError(RuntimeError):
    pass


def post(settings: SlackSettings, payload: dict[str, object]) -> None:
    url = settings.slack_webhook_url.get_secret_value()
    if not url.startswith("https://hooks.slack.com/"):
        msg = "SLACK_WEBHOOK_URL is not a Slack incoming webhook URL"
        raise NotifyError(msg)
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"})  # noqa: S310 - checked to be a Slack https URL above
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 - see above
        if response.status != 200:  # noqa: PLR2004 - Slack answers 200 "ok"
            msg = f"Slack answered {response.status}"
            raise NotifyError(msg)
