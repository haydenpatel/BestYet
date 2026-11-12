"""Thin synchronous Grist REST client. Errors never carry headers, requests or the API key."""

import json
import socket
import ssl
from typing import Any, Self
from urllib.parse import quote

import httpx

from bestyet.config import Secret

DEFAULT_TIMEOUT = 15.0


def _q(value: object) -> str:
    return quote(str(value), safe="")


class GristError(Exception):
    """A failed Grist call. `status` is the HTTP status, or 0 when no response arrived.

    `cause` classifies a failure with no usable answer: "dns", "refused", "timeout", "tls",
    "other" (no response), "redirect" or "invalid_json"; None for an ordinary HTTP error."""

    def __init__(self, status: int, message: str, cause: str | None = None):
        super().__init__(f"Grist error {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.cause = cause


def _cause(exc: BaseException) -> str:
    """Classify a transport failure by walking its cause chain."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return "dns"
        if isinstance(current, ConnectionRefusedError):
            return "refused"
        if isinstance(current, ssl.SSLError):
            return "tls"
        current = current.__cause__ or current.__context__
    return "other"


def _error_message(response: httpx.Response) -> str:
    if response.is_redirect:
        return "Unexpected redirect: check the Grist URL (use its final http(s) address)"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return response.reason_phrase or "request failed"


class GristClient:
    """Calls one Grist document. The key is only ever passed in, never read from the environment."""

    def __init__(
        self,
        base_url: str,
        doc_id: str,
        api_key: Secret,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self._doc_id = _q(doc_id)
        url = httpx.URL(base_url)
        self._origin = f"{url.scheme}://{url.netloc.decode().rpartition('@')[2]}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/",
            headers={"Authorization": f"Bearer {api_key.reveal()}"},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"GristClient(base_url={self._origin!r}, doc_id={self._doc_id!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, path: str, *, params: dict | None = None, body: Any = None):
        # Raise only after the except block so no httpx exception (which holds the request and
        # its headers) is left as __context__.
        failure = cause = None
        try:
            response = self._http.request(method, path, params=params, json=body)
        except httpx.TimeoutException as e:
            failure, cause = "Grist request timed out", _cause(e)
        except httpx.HTTPError as e:
            failure, cause = "Could not reach Grist", _cause(e)
        if failure:
            raise GristError(0, failure, cause)
        if not response.is_success:
            raise GristError(
                response.status_code,
                _error_message(response),
                "redirect" if response.is_redirect else None,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            failure = "Grist returned invalid JSON"
        raise GristError(response.status_code, failure, "invalid_json")

    def _doc(self, path: str, **kwargs):
        return self._request(kwargs.pop("method", "GET"), f"docs/{self._doc_id}/{path}", **kwargs)

    # Records. Each record is `{"id": N, "fields": {...}}`, as Grist returns it.

    def records(self, table: str, filter: dict | None = None, sort: str | None = None) -> list:
        params = {}
        if filter:
            params["filter"] = json.dumps(filter)
        if sort:
            params["sort"] = sort
        return self._doc(f"tables/{_q(table)}/records", params=params)["records"]

    def add(self, table: str, records: list[dict]) -> list[int]:
        """Add records given as field dicts. Returns the new row ids."""
        body = {"records": [{"fields": fields} for fields in records]}
        result = self._doc(f"tables/{_q(table)}/records", method="POST", body=body)
        return [r["id"] for r in result["records"]]

    def patch(self, table: str, records: list[dict]) -> None:
        """Update records given as `{"id": N, "fields": {...}}`."""
        self._doc(f"tables/{_q(table)}/records", method="PATCH", body={"records": records})

    def delete(self, table: str, ids: list[int]) -> None:
        self._doc(f"tables/{_q(table)}/data/delete", method="POST", body=ids)

    def sql(self, sql: str, args: list | None = None) -> list[dict]:
        """Run a SELECT. Returns one dict of column values per row."""
        body: dict[str, Any] = {"sql": sql}
        if args:
            body["args"] = args
        return [r["fields"] for r in self._doc("sql", method="POST", body=body)["records"]]

    # Schema and UserActions.

    def tables(self) -> list:
        return self._doc("tables")["tables"]

    def columns(self, table: str) -> list:
        return self._doc(f"tables/{_q(table)}/columns")["columns"]

    def apply(self, actions: list[list]) -> Any:
        """Apply UserActions, each a list such as `["AddTable", "T", [...]]`."""
        return self._doc("apply", method="POST", body=actions)

    def add_tables(self, tables: list[dict]) -> list[str]:
        """Add tables given as `{"id": "T", "columns": [{"id": "C", "fields": {...}}]}`.

        Returns the ids Grist assigned, which can differ from the requested ones.
        """
        result = self._doc("tables", method="POST", body={"tables": tables})
        return [t["id"] for t in result["tables"]]

    def add_columns(self, table: str, columns: list[dict]) -> list[str]:
        """Add columns given as `{"id": "C", "fields": {...}}`. Returns the assigned ids."""
        body = {"columns": columns}
        result = self._doc(f"tables/{_q(table)}/columns", method="POST", body=body)
        return [c["id"] for c in result["columns"]]

    def update_columns(self, table: str, columns: list[dict]) -> None:
        """Update columns given as `{"id": "C", "fields": {...}}`."""
        body = {"columns": columns}
        self._doc(f"tables/{_q(table)}/columns", method="PATCH", body=body)

    # Organisations, workspaces and documents (init script).

    def orgs(self) -> list:
        return self._request("GET", "orgs")

    def workspaces(self, org: int | str) -> list:
        return self._request("GET", f"orgs/{_q(org)}/workspaces")

    def create_doc(self, workspace_id: int, name: str) -> str:
        """Create an empty document. Returns its id."""
        return self._request("POST", f"workspaces/{_q(workspace_id)}/docs", body={"name": name})
