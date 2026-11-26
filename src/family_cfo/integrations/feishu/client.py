"""Feishu/Lark Bitable REST client.

Implements only the endpoints Family CFO needs, with:

* tenant_access_token caching (2h token, refresh 5 min before expiry)
* per-second rate limiting
* retry with exponential backoff on the official retryable codes
* no secret logging; errors carry the API code but never the token
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ...core.config import Settings
from ...core.errors import FeishuApiError, FeishuAuthError, FeishuNotConfigured
from ...core.logging import get_logger

log = get_logger("feishu.client")

RETRYABLE_CODES = {1254290, 1254291, 1254607, 1255040}
TOKEN_INVALID_CODES = {99991661, 99991663, 99991668}
RATE_LIMIT_CODE = 1254290

AUTH_PATH = "/open-apis/auth/v3/tenant_access_token/internal"


@dataclass
class TokenState:
    token: str = ""
    expires_at: float = 0.0


class RateLimiter:
    """Simple monotonic-clock limiter (no sleeping in unit tests)."""

    def __init__(self, per_second: int, *, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.per_second = max(per_second, 1)
        self.clock = clock
        self.sleeper = sleeper
        self.window_start = 0.0
        self.count = 0

    def acquire(self) -> None:
        now = self.clock()
        if now - self.window_start >= 1.0:
            self.window_start = now
            self.count = 0
        if self.count >= self.per_second:
            wait = max(1.0 - (now - self.window_start), 0.0)
            if wait > 0:
                self.sleeper(wait)
            self.window_start = self.clock()
            self.count = 0
        self.count += 1


class FeishuClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.sleeper = sleeper
        self.clock = clock
        self._token = TokenState()
        self._limiter = RateLimiter(settings.feishu_rate_limit, clock=clock, sleeper=sleeper)
        self._http = httpx.Client(
            base_url=settings.feishu_base_url,
            timeout=settings.feishu_timeout,
            transport=transport,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    # ------------------------------------------------------------------ public
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def configured(self) -> bool:
        return self.settings.feishu_configured

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "base_url": self.settings.feishu_base_url,
            "app_token_set": bool(self.settings.feishu_app_token),
            "token_cached": bool(self._token.token and self.clock() < self._token.expires_at),
            "connected": False,
        }

    def healthcheck(self) -> dict:
        if not self.configured:
            raise FeishuNotConfigured(
                "飞书未配置：请设置 FAMILY_CFO_FEISHU_APP_ID / APP_SECRET / APP_TOKEN"
            )
        self._ensure_token()
        data = self.list_tables()
        return {"connected": True, "tables": len(data), "app_token_masked": self._mask(self.settings.feishu_app_token)}

    # ------------------------------------------------------------------- token
    def _ensure_token(self) -> str:
        if self._token.token and self.clock() < self._token.expires_at - 300:
            return self._token.token
        if not (self.settings.feishu_app_id and self.settings.feishu_app_secret):
            raise FeishuNotConfigured(
                "飞书应用未配置：请设置 FAMILY_CFO_FEISHU_APP_ID 与 FAMILY_CFO_FEISHU_APP_SECRET"
            )
        self._limiter.acquire()
        try:
            response = self._http.post(
                AUTH_PATH,
                json={
                    "app_id": self.settings.feishu_app_id,
                    "app_secret": self.settings.feishu_app_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise FeishuAuthError("无法连接飞书鉴权接口", detail=str(exc)) from exc
        data = _parse(response)
        if data.get("code") != 0:
            raise FeishuAuthError(
                "获取 tenant_access_token 失败",
                detail=f"code={data.get('code')} msg={data.get('msg')}",
            )
        token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 7200))
        if not token:
            raise FeishuAuthError("飞书返回了空 token")
        self._token = TokenState(token=token, expires_at=self.clock() + expire)
        return token

    def _invalidate_token(self) -> None:
        self._token = TokenState()

    # ----------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        authenticated: bool = True,
    ) -> dict:
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            headers = {}
            if authenticated:
                headers["Authorization"] = f"Bearer {self._ensure_token()}"
            self._limiter.acquire()
            try:
                response = self._http.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                if attempt > self.settings.feishu_max_retries:
                    raise FeishuApiError("飞书请求失败（网络）", detail=str(exc)) from exc
                self._backoff(attempt)
                continue
            try:
                payload = _parse(response)
            except FeishuApiError:
                if attempt > self.settings.feishu_max_retries:
                    raise
                self._backoff(attempt)
                continue
            code = payload.get("code", 0)
            if code == 0:
                return payload
            if code in TOKEN_INVALID_CODES and not refreshed:
                refreshed = True
                self._invalidate_token()
                continue
            if code in RETRYABLE_CODES and attempt <= self.settings.feishu_max_retries:
                self._backoff(attempt)
                continue
            raise FeishuApiError(
                f"飞书接口返回错误 code={code}: {payload.get('msg', '')}",
                api_code=code,
                detail=str(payload.get("error")) if payload.get("error") else None,
            )

    def _backoff(self, attempt: int) -> None:
        delay = min(0.5 * (2 ** (attempt - 1)), 8.0)
        log.warning("feishu retry #%s after %.1fs", attempt, delay)
        self.sleeper(delay)

    # -------------------------------------------------------------- bitable API
    def list_tables(self) -> list[dict]:
        payload = self.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables",
            params={"page_size": 100},
        )
        return payload.get("data", {}).get("items", [])

    def create_table(self, name: str, fields: list[dict]) -> dict:
        payload = self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables",
            json={"table": {"name": name, "default_view_name": "全部", "fields": fields}},
        )
        return payload.get("data", {}).get("table", {})

    def list_fields(self, table_id: str) -> list[dict]:
        payload = self.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/fields",
            params={"page_size": 100},
        )
        return payload.get("data", {}).get("items", [])

    def create_field(self, table_id: str, field: dict) -> dict:
        payload = self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/fields",
            json=field,
        )
        return payload.get("data", {}).get("field", {})

    def search_records(
        self,
        table_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 500,
        filter: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"automatic_fields": True}
        if filter:
            body["filter"] = filter
        payload = self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records/search",
            params={"page_size": min(page_size, 500), **({"page_token": page_token} if page_token else {})},
            json=body,
        )
        return payload.get("data", {})

    def list_records(self, table_id: str, *, page_token: str | None = None, page_size: int = 500) -> dict:
        payload = self.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records",
            params={"page_size": min(page_size, 500), **({"page_token": page_token} if page_token else {})},
        )
        return payload.get("data", {})

    def get_record(self, table_id: str, record_id: str) -> dict:
        payload = self.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records/{record_id}",
        )
        return payload.get("data", {}).get("record", {})

    def create_record(self, table_id: str, fields: dict) -> dict:
        payload = self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records",
            json={"fields": fields},
        )
        return payload.get("data", {}).get("record", {})

    def batch_create(self, table_id: str, records: list[dict]) -> list[dict]:
        if len(records) > 500:
            raise FeishuApiError("单次批量写入不能超过 500 条")
        payload = self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records/batch_create",
            json={"records": [{"fields": r} for r in records]},
        )
        return payload.get("data", {}).get("records", [])

    def update_record(self, table_id: str, record_id: str, fields: dict) -> dict:
        payload = self.request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records/{record_id}",
            json={"fields": fields},
        )
        return payload.get("data", {}).get("record", {})

    def delete_record(self, table_id: str, record_id: str) -> None:
        self.request(
            "DELETE",
            f"/open-apis/bitable/v1/apps/{self.settings.feishu_app_token}/tables/{table_id}/records/{record_id}",
        )

    def iter_records(self, table_id: str) -> list[dict]:
        items: list[dict] = []
        page_token = None
        while True:
            data = self.list_records(table_id, page_token=page_token)
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return items

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        return value[:3] + "***" + value[-2:] if len(value) > 6 else "***"


def _parse(response: httpx.Response) -> dict:
    if response.status_code >= 500:
        raise FeishuApiError(f"飞书服务端错误 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuApiError(
            f"飞书返回非 JSON 响应 HTTP {response.status_code}",
            detail=response.text[:200],
        ) from exc
    if not isinstance(payload, dict):
        raise FeishuApiError("飞书返回结构异常")
    if response.status_code >= 400 and payload.get("code") in (None, 0):
        raise FeishuApiError(f"飞书请求失败 HTTP {response.status_code}")
    return payload
