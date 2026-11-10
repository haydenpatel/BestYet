"""Configuration, read only from environment variables."""

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PORT = 8585
DEFAULT_TZ = "UTC"


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid. Names variables, never values."""


class Secret:
    """Holds a secret string. Prints masked; use `reveal()` to read it."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"

    __str__ = __repr__


def load_dotenv(path: str | Path = ".env", environ: MutableMapping[str, str] | None = None) -> None:
    """Load KEY=VALUE lines from a dev `.env` file into `environ` (default: os.environ).

    The path is relative to the current working directory. Supports an `export ` prefix,
    quoted values, and ` #` comments after unquoted values. Variables that are already set
    to a non-empty value are never overridden; empty ones count as unset.
    """
    environ = os.environ if environ is None else environ
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            value = value[1 : value.index(value[0], 1)]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        if key and not environ.get(key):
            environ[key] = value


def _valid_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on an invalid port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and not (parts.query or parts.fragment)
    )


@dataclass(frozen=True)
class Config:
    grist_url: str
    grist_doc_id: str
    grist_api_key: Secret
    port: int = DEFAULT_PORT
    tz: str = DEFAULT_TZ

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """Build a Config. With no mapping, loads a dev `.env` and reads os.environ."""
        if environ is None:
            load_dotenv()
            environ = os.environ
        problems: list[str] = []

        def get(name: str) -> str:
            return (environ.get(name) or "").strip()

        url = get("GRIST_URL").rstrip("/")
        if not url:
            problems.append("GRIST_URL is missing")
        elif not _valid_url(url):
            problems.append("GRIST_URL must be an http(s) URL with a host and no query or fragment")

        doc_id = get("GRIST_DOC_ID")
        if not doc_id:
            problems.append("GRIST_DOC_ID is missing")

        api_key = get("GRIST_API_KEY")
        if not api_key:
            problems.append("GRIST_API_KEY is missing")

        port = DEFAULT_PORT
        raw_port = environ.get("PORT") or ""
        if raw_port:
            if raw_port.isascii() and raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
                port = int(raw_port)
            else:
                problems.append("PORT must be an integer from 1 to 65535")

        tz = get("TZ") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            problems.append("TZ is not a valid time zone name")

        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))
        return cls(url, doc_id, Secret(api_key), port, tz)
