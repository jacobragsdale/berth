"""Datadog metrics gateway for the rightsizing bot: one timeseries query, returned as timestamped values.

Written against Datadog's documented v1 query API (GET /api/v1/query); not yet run against a real account.
"""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_TIMEOUT_SECONDS = 60
MAX_POINTS = 1400  # Datadog coarsens a series beyond about 1,500 points; choose the rollup so it never has to

type Transport = Callable[[str, Mapping[str, str]], object]
"""(url, headers) -> parsed JSON response, for a GET."""


class DatadogSettings(BaseSettings):
    """DD_API_KEY and DD_APP_KEY, from a Key Vault-linked variable group. DD_SITE is the account's site."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)
    dd_api_key: SecretStr
    dd_app_key: SecretStr
    dd_site: str = "datadoghq.com"


class DatadogError(RuntimeError):
    pass


class _Series(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    pointlist: tuple[tuple[float, float | None], ...] = ()


class _Reply(BaseModel):
    """Only the fields berth reads. Datadog adds fields freely, so unknown ones are ignored, not refused."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    status: str
    error: str | None = None
    series: tuple[_Series, ...] = ()


def http_transport() -> Transport:
    def get(url: str, headers: Mapping[str, str]) -> object:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310 - https URL built from the configured site
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 - see above
            parsed: object = json.loads(response.read())
        return parsed

    return get


def rollup_seconds(span_seconds: int) -> int:
    """The coarsest rollup, in whole minutes and at least five, that keeps a series under MAX_POINTS."""
    return max(300, -(-span_seconds // MAX_POINTS // 60) * 60)


@dataclass(frozen=True)
class Datadog:
    settings: DatadogSettings
    transport: Transport

    def points(self, query: str, start: int, end: int, *, rollup: str = "max") -> list[tuple[float, float]]:
        """Every non-empty point of every series the query returns, as (unix seconds, value)."""
        full = f"{query}.rollup({rollup}, {rollup_seconds(end - start)})"
        url = f"https://api.{self.settings.dd_site}/api/v1/query?" + urllib.parse.urlencode({"from": start, "to": end, "query": full})
        headers = {"DD-API-KEY": self.settings.dd_api_key.get_secret_value(), "DD-APPLICATION-KEY": self.settings.dd_app_key.get_secret_value()}
        reply = _Reply.model_validate(self.transport(url, headers))
        if reply.status != "ok":
            msg = f"Datadog query failed: {reply.error or reply.status} ({full})"
            raise DatadogError(msg)
        return [(timestamp / 1000, value) for series in reply.series for timestamp, value in series.pointlist if value is not None]
