"""In-memory Feishu Bitable server for tests.

Mirrors the REST shapes used by :class:`FeishuClient` so the real client code
(serialisation, retry, pagination, conflict detection) is exercised without
network access or credentials.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

AUTH_RE = re.compile(r"^/open-apis/auth/v3/tenant_access_token/internal$")
TABLES_RE = re.compile(r"^/open-apis/bitable/v1/apps/([^/]+)/tables$")
FIELDS_RE = re.compile(r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/fields$")
RECORDS_RE = re.compile(r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/records$")
SEARCH_RE = re.compile(r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/records/search$")
BATCH_CREATE_RE = re.compile(
    r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/records/batch_create$"
)
BATCH_UPDATE_RE = re.compile(
    r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/records/batch_update$"
)
RECORD_RE = re.compile(
    r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/records/([^/]+)$"
)
VIEWS_RE = re.compile(r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/views$")
VIEW_RE = re.compile(
    r"^/open-apis/bitable/v1/apps/([^/]+)/tables/([^/]+)/views/([^/]+)$"
)


@dataclass
class MockTable:
    table_id: str
    name: str
    fields: list[dict] = field(default_factory=list)
    records: dict[str, dict] = field(default_factory=dict)
    views: list[dict] = field(default_factory=list)


class MockFeishuServer:
    def __init__(self, *, app_id: str = "cli_mock", app_secret: str = "secret") -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.tables: dict[str, MockTable] = {}
        self.calls: list[tuple[str, str]] = []
        self._seq = 0
        self._clock_ms = int(datetime.now().timestamp() * 1000)
        self._failures: list[dict] = []
        self.token_requests = 0

    # ------------------------------------------------------------- test helpers
    def fail_next(self, code: int, *, times: int = 1, message: str = "injected") -> None:
        for _ in range(times):
            self._failures.append({"code": code, "msg": message})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def table_by_name(self, name: str) -> MockTable:
        for table in self.tables.values():
            if table.name == name:
                return table
        raise KeyError(name)

    def find_record(self, table_name: str, field_name: str, value: Any) -> dict | None:
        table = self.table_by_name(table_name)
        for record in table.records.values():
            if record["fields"].get(field_name) == value:
                return record
        return None

    def edit_remote(self, table_name: str, record_id: str, fields: dict) -> dict:
        table = self.table_by_name(table_name)
        record = table.records[record_id]
        record["fields"].update(fields)
        record["last_modified_time"] = self._tick()
        return record

    # ------------------------------------------------------------------ routing
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        query = parse_qs(urlparse(str(request.url)).query)
        self.calls.append((request.method, path))
        if self._failures:
            failure = self._failures.pop(0)
            return _ok(failure)

        if AUTH_RE.match(path):
            body = _body(request)
            if body.get("app_id") != self.app_id or body.get("app_secret") != self.app_secret:
                return _ok({"code": 10003, "msg": "invalid app_id or app_secret"})
            self.token_requests += 1
            return _ok(
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "t-mock-" + str(self.token_requests),
                    "expire": 7200,
                }
            )

        match = TABLES_RE.match(path)
        if match and request.method == "GET":
            return _ok(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "items": [
                            {"table_id": t.table_id, "name": t.name} for t in self.tables.values()
                        ],
                        "has_more": False,
                    },
                }
            )
        if match and request.method == "POST":
            body = _body(request).get("table", {})
            table_id = self._new_id("tbl")
            table = MockTable(table_id=table_id, name=body.get("name", "table"))
            for index, f in enumerate(body.get("fields", []), start=1):
                table.fields.append(
                    {
                        "field_id": f"fld{index}",
                        "field_name": f.get("field_name"),
                        "type": f.get("type", 1),
                        "property": f.get("property"),
                    }
                )
            self.tables[table_id] = table
            # Real API returns {"data": {"table_id": ..., "default_view_id": ...}}
            # (no data.table layer) — mirror that exactly.
            return _ok(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"table_id": table_id, "default_view_id": "vew" + table_id},
                }
            )

        match = FIELDS_RE.match(path)
        if match and request.method == "GET":
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            return _ok(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"items": table.fields, "has_more": False},
                }
            )
        if match and request.method == "POST":
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            body = _body(request)
            field_def = {
                "field_id": f"fld{len(table.fields) + 1}",
                "field_name": body.get("field_name"),
                "type": body.get("type", 1),
                "property": body.get("property"),
            }
            table.fields.append(field_def)
            return _ok({"code": 0, "msg": "success", "data": {"field": field_def}})

        match = SEARCH_RE.match(path)
        if match and request.method == "POST":
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            items = list(table.records.values())
            return _ok(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"items": items, "has_more": False, "total": len(items)},
                }
            )

        match = BATCH_CREATE_RE.match(path)
        if match and request.method == "POST":
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            body = _body(request)
            records = []
            for item in body.get("records", []):
                record = self._create_record(table, item.get("fields", {}))
                records.append(record)
            return _ok({"code": 0, "msg": "success", "data": {"records": records}})

        match = BATCH_UPDATE_RE.match(path)
        if match and request.method == "POST":
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            body = _body(request)
            updated = []
            for item in body.get("records", []):
                record = table.records.get(item.get("record_id"))
                if record is None:
                    return _code(1254043, "record not found")
                record["fields"].update(item.get("fields", {}))
                record["last_modified_time"] = self._tick()
                updated.append(record)
            return _ok({"code": 0, "msg": "success", "data": {"records": updated}})

        match = RECORDS_RE.match(path)
        if match:
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            if request.method == "GET":
                page_size = int(query.get("page_size", ["20"])[0])
                items = list(table.records.values())
                return _ok(
                    {
                        "code": 0,
                        "msg": "success",
                        "data": {
                            "items": items[:page_size],
                            "has_more": len(items) > page_size,
                            "total": len(items),
                        },
                    }
                )
            if request.method == "POST":
                record = self._create_record(table, _body(request).get("fields", {}))
                return _ok({"code": 0, "msg": "success", "data": {"record": record}})

        match = RECORD_RE.match(path)
        if match:
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            record = table.records.get(match.group(3))
            if record is None:
                return _code(1254043, "record not found")
            if request.method == "GET":
                return _ok({"code": 0, "msg": "success", "data": {"record": record}})
            if request.method == "PUT":
                record["fields"].update(_body(request).get("fields", {}))
                record["last_modified_time"] = self._tick()
                return _ok({"code": 0, "msg": "success", "data": {"record": record}})
            if request.method == "DELETE":
                del table.records[match.group(3)]
                return _ok({"code": 0, "msg": "success", "data": {}})

        match = VIEWS_RE.match(path)
        if match:
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            if request.method == "GET":
                return _ok(
                    {
                        "code": 0,
                        "msg": "success",
                        "data": {"items": table.views, "has_more": False},
                    }
                )
            if request.method == "POST":
                body = _body(request)
                view = {
                    "view_id": self._new_id("vew"),
                    "view_name": body.get("view_name", "grid"),
                    "view_type": body.get("view_type", "grid"),
                    "filter_info": None,
                }
                table.views.append(view)
                return _ok({"code": 0, "msg": "success", "data": {"view": view}})

        match = VIEW_RE.match(path)
        if match:
            table = self._table(match.group(2))
            if table is None:
                return _code(1254041, "table not found")
            view = next(
                (v for v in table.views if v["view_id"] == match.group(3)), None
            )
            if view is None:
                return _code(1254041, "view not found")
            if request.method == "PATCH":
                body = _body(request)
                if "filter_info" in body:
                    view["filter_info"] = body["filter_info"]
                return _ok({"code": 0, "msg": "success", "data": {"view": view}})
            if request.method == "DELETE":
                table.views.remove(view)
                return _ok({"code": 0, "msg": "success", "data": {}})

        return _ok({"code": 404, "msg": f"mock route not implemented: {request.method} {path}"})

    # ------------------------------------------------------------------ helpers
    def _table(self, table_id: str) -> MockTable | None:
        return self.tables.get(table_id)

    def _create_record(self, table: MockTable, fields: dict) -> dict:
        record_id = self._new_id("rec")
        record = {
            "record_id": record_id,
            "fields": dict(fields),
            "created_time": self._tick(),
            "last_modified_time": self._clock_ms,
        }
        table.records[record_id] = record
        return record

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:08d}"

    def _tick(self) -> int:
        self._clock_ms += 1000
        return self._clock_ms


def _body(request: httpx.Request) -> dict:
    try:
        return json.loads(request.content.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


def _ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _code(code: int, msg: str) -> httpx.Response:
    return httpx.Response(200, json={"code": code, "msg": msg})
