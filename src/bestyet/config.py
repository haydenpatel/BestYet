"""Configuration, read only from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PORT = 8585
DEFAULT_TZ = "UTC"


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid. Names variables, never values."""


def load_dotenv(path: str | Path = ".env", environ: dict | None = None) -> None:
    """Load KEY=VALUE lines from a dev `.env` file. Never overrides existing variables."""
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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    grist_url: str
    grist_doc_id: str
    grist_api_key: str = field(repr=False)
    port: int = DEFAULT_PORT
    tz: str = DEFAULT_TZ

    @property
    def masked_api_key(self) -> str:
        return "***" if self.grist_api_key else ""

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        return (
            f"Config(grist_url={self.grist_url!r}, grist_doc_id={self.grist_doc_id!r}, "
            f"grist_api_key={self.masked_api_key!r}, port={self.port!r}, tz={self.tz!r})"
        )

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
        elif not url.startswith(("http://", "https://")):
            problems.append("GRIST_URL must start with http:// or https://")

        doc_id = get("GRIST_DOC_ID")
        if not doc_id:
            problems.append("GRIST_DOC_ID is missing")

        api_key = get("GRIST_API_KEY")
        if not api_key:
            problems.append("GRIST_API_KEY is missing")

        port = DEFAULT_PORT
        if get("PORT"):
            try:
                port = int(get("PORT"))
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                problems.append("PORT must be an integer from 1 to 65535")

        tz = get("TZ") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            problems.append("TZ is not a valid time zone name")

        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))
        return cls(url, doc_id, api_key, port, tz)
