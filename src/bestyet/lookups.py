"""Resolve Athlete, Event and Meet names to Grist row ids, and back.

Each table is loaded once and kept briefly in memory (the data is also edited
directly in Grist). A name that is not found triggers one reload of that table
before it counts as unknown, so a row just added resolves at once. Names match exactly
(case-sensitive) after trimming surrounding whitespace.
"""

import time
from collections.abc import Callable
from typing import Protocol

DEFAULT_TTL = 30.0


class Records(Protocol):
    def records(self, table: str) -> list: ...


class LookupFailed(LookupError):
    """A name that matches no row, or more than one. The message is safe to show."""


class Lookups:
    def __init__(
        self,
        client: Records,
        *,
        ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._ttl = ttl
        self._clock = clock
        # table -> (loaded at, id -> name, name -> ids)
        self._cache: dict[str, tuple[float, dict[int, str], dict[str, list[int]]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def _load(self, table: str, *, force: bool = False):
        entry = self._cache.get(table)
        if entry and not force and self._clock() - entry[0] < self._ttl:
            return entry
        names: dict[int, str] = {}
        ids: dict[str, list[int]] = {}
        for record in self._client.records(table):
            name = record["fields"].get("Name")
            if isinstance(name, str) and name.strip():
                name = name.strip()
                names[record["id"]] = name
                ids.setdefault(name, []).append(record["id"])
        entry = self._cache[table] = (self._clock(), names, ids)
        return entry

    def _id(self, table: str, label: str, name: str) -> int:
        name = name.strip()
        loaded = table in self._cache and self._clock() - self._cache[table][0] < self._ttl
        matches = self._load(table)[2].get(name, [])
        if not matches and loaded:
            matches = self._load(table, force=True)[2].get(name, [])
        if not matches:
            raise LookupFailed(f"No {label} named {name!r}")
        if len(matches) > 1:
            raise LookupFailed(f"More than one {label} is named {name!r}")
        return matches[0]

    def athlete_id(self, name: str) -> int:
        return self._id("Athletes", "athlete", name)

    def event_id(self, name: str) -> int:
        return self._id("Events", "event", name)

    def meet_id(self, name: str) -> int:
        return self._id("Meets", "meet", name)

    def athlete_name(self, row_id: int) -> str | None:
        return self._load("Athletes")[1].get(row_id)

    def event_name(self, row_id: int) -> str | None:
        return self._load("Events")[1].get(row_id)

    def meet_name(self, row_id: int) -> str | None:
        return self._load("Meets")[1].get(row_id)
