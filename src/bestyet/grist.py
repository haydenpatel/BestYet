"""Thin synchronous Grist REST client. Errors never carry headers, requests or the API key."""

import json
from typing import Any, Self
from urllib.parse import quote

import httpx

from bestyet.config import Secret

DEFAULT_TIMEOUT = 15.0


def _q(value: object) -> str:
    return quote(str(value), safe="")


class GristError(Exception):
    """A failed Grist call. `status` is the HTTP status, or 0 when no response arrived."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Grist error {status}: {message}" if status else message)
        self.status = status
        self.message = message


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
        failure = None
        try:
            response = self._http.request(method, path, params=params, json=body)
        except httpx.TimeoutException:
            failure = "Grist request timed out"
        except httpx.HTTPError:
            failure = "Could not reach Grist"
        if failure:
            raise GristError(0, failure)
        if not response.is_success:
            raise GristError(response.status_code, _error_message(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            failure = "Grist returned invalid JSON"
        raise GristError(response.status_code, failure)

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

    # Organisations, workspaces and documents (init script).

    def orgs(self) -> list:
        return self._request("GET", "orgs")

    def workspaces(self, org: int | str) -> list:
        return self._request("GET", f"orgs/{_q(org)}/workspaces")

    def create_doc(self, workspace_id: int, name: str) -> str:
        """Create an empty document. Returns its id."""
        return self._request("POST", f"workspaces/{_q(workspace_id)}/docs", body={"name": name})
