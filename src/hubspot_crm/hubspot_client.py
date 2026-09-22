from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from hubspot_crm.config import Settings

HUBSPOT_BASE_URL = "https://api.hubapi.com"

COMPANY_PROPERTIES = [
    "name",
    "domain",
    "hubspot_owner_id",
    "lifecyclestage",
    "createdate",
    "hs_object_source",
    "hs_object_source_label",
    "hs_object_source_detail_1",
    "hs_object_source_detail_2",
    "hs_object_source_detail_3",
    "hs_analytics_source",
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",
    "notes_last_contacted",
    "notes_last_updated",
    "hs_last_sales_activity_timestamp",
    "hs_last_booked_meeting_date",
    "engagements_last_meeting_booked",
    "hs_last_logged_call_date",
    "hs_last_logged_outgoing_email_date",
    "hs_sales_email_last_replied",
    "hs_latest_meeting_activity",
    "hs_last_sales_activity_type",
]

CONTACT_PROPERTIES = [
    "email",
    "work_email",
    "hs_additional_emails",
    "firstname",
    "lastname",
    "jobtitle",
    "lifecyclestage",
    "createdate",
    "hs_object_source",
    "hs_object_source_label",
    "hs_object_source_detail_1",
    "hs_object_source_detail_2",
    "hs_object_source_detail_3",
    "hs_analytics_source",
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",
    "notes_last_contacted",
    "notes_last_updated",
    "hs_last_sales_activity_timestamp",
    "hs_last_booked_meeting_date",
    "engagements_last_meeting_booked",
    "hs_last_logged_call_date",
    "hs_last_logged_outgoing_email_date",
    "hs_sales_email_last_replied",
    "hs_latest_meeting_activity",
    "hs_last_sales_activity_type",
]

DEAL_PROPERTIES = [
    "dealname",
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "createdate",
    "hs_lastmodifieddate",
    "hs_is_closed",
    "hs_is_closed_won",
    "hs_is_closed_lost",
    "hs_closed_lost_reason",
]

class HubSpotReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        category: str = "request_error",
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        self.retryable = retryable
        self.headers = dict(headers or {})


class RequestCapExceeded(HubSpotReadError):
    pass


@dataclass
class RequestMetrics:
    requests: int = 0
    retries: int = 0
    successes: int = 0
    errors: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    endpoint_counts: dict[str, int] = field(default_factory=dict)
    rate_limit_headers: dict[str, str] = field(default_factory=dict)

    def record(self, endpoint: str, status: int, headers: dict[str, str]) -> None:
        self.requests += 1
        self.status_counts[str(status)] = self.status_counts.get(str(status), 0) + 1
        self.endpoint_counts[endpoint] = self.endpoint_counts.get(endpoint, 0) + 1
        for key, value in headers.items():
            lowered = key.lower()
            if lowered.startswith("x-hubspot-ratelimit") or lowered == "retry-after":
                self.rate_limit_headers[lowered] = str(value)
        if 200 <= status < 300:
            self.successes += 1
        else:
            self.errors += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "successes": self.successes,
            "errors": self.errors,
            "status_counts": dict(self.status_counts),
            "endpoint_counts": dict(self.endpoint_counts),
            "rate_limit_headers": dict(self.rate_limit_headers),
        }


Transport = Callable[
    [str, str, dict[str, str], dict[str, Any] | None, dict[str, Any] | None, int],
    tuple[int, dict[str, str], dict[str, Any]],
]


def _safe_error_message(status: int, body: str) -> str:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = {}
    message = str(parsed.get("message") or parsed.get("category") or "request failed")
    correlation = str(parsed.get("correlationId") or "")
    suffix = f" correlation_id={correlation}" if correlation else ""
    return f"HubSpot read failed status={status}: {message[:300]}{suffix}"


def _urlopen_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    query: dict[str, Any] | None,
    body: dict[str, Any] | None,
    timeout: int,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    if query:
        encoded = urlencode(
            [(key, value) for key, raw in query.items() if raw is not None for value in (raw if isinstance(raw, list) else [raw])]
        )
        url += ("&" if "?" in url else "?") + encoded
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=payload, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
            response_headers = {key: value for key, value in response.headers.items()}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        response_headers = {key: value for key, value in exc.headers.items()}
        raise HubSpotReadError(
            _safe_error_message(status, raw),
            status=status,
            category="authorization" if status in {401, 403} else "http_error",
            retryable=status == 429 or 500 <= status <= 599,
            headers=response_headers,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HubSpotReadError(
            f"HubSpot transport error: {type(exc).__name__}",
            category="transport_error",
            retryable=True,
        ) from exc
    if not raw:
        return status, response_headers, {}
    parsed = json.loads(raw)
    return status, response_headers, parsed if isinstance(parsed, dict) else {"data": parsed}


def _endpoint_label(method: str, path: str) -> str:
    parts = [
        "{id}" if part.isdigit() or (len(part) > 8 and any(character.isdigit() for character in part)) else part
        for part in path.split("/")
    ]
    return f"{method.upper()} {'/'.join(parts)}"


class HubSpotClient:
    """Bounded, sequential, read-only HubSpot client."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_cap: int | None = None,
    ) -> None:
        if not settings.hubspot_access_token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN is required")
        self.settings = settings
        self.transport = transport or _urlopen_transport
        self.sleep = sleep
        self.extra_company_properties: list[str] = []
        self.request_cap = request_cap if request_cap is not None else settings.hubspot_request_cap
        self.metrics = RequestMetrics()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.hubspot_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hubspot-crm-scoring/0.1",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError("HubSpotClient is read-only; only GET and search/batch POST are allowed")
        if method == "POST" and not (
            path.endswith("/search") or path.endswith("/batch/read")
        ):
            raise ValueError("HubSpotClient blocks POST endpoints that can mutate CRM records")
        endpoint = _endpoint_label(method, path)
        attempts = max(1, self.settings.hubspot_retry_attempts)
        last_error: HubSpotReadError | None = None
        for attempt in range(attempts):
            if self.metrics.requests >= self.request_cap:
                raise RequestCapExceeded(
                    f"HubSpot request cap reached ({self.request_cap})",
                    category="request_cap",
                )
            try:
                status, headers, payload = self.transport(
                    method,
                    HUBSPOT_BASE_URL + path,
                    self.headers,
                    query,
                    body,
                    timeout,
                )
                self.metrics.record(endpoint, status, headers)
                return payload
            except HubSpotReadError as exc:
                self.metrics.record(endpoint, exc.status, exc.headers)
                last_error = exc
                if not exc.retryable or attempt + 1 >= attempts:
                    raise
                self.metrics.retries += 1
                delay = self._retry_delay(exc, attempt)
                self.sleep(delay)
        raise last_error or HubSpotReadError("HubSpot request failed")

    def _retry_delay(self, error: HubSpotReadError, attempt: int) -> float:
        retry_after = next(
            (
                value
                for key, value in error.headers.items()
                if key.lower() == "retry-after"
            ),
            "",
        )
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                    return min(60.0, max(0.0, (parsed - datetime.now(parsed.tzinfo)).total_seconds()))
                except (TypeError, ValueError):
                    pass
        base = max(0, self.settings.hubspot_retry_backoff_seconds)
        return min(30.0, base * (2**attempt) + random.uniform(0, 0.25))

    def property_definitions(self, object_type: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/crm/v3/properties/{object_type}", query={"archived": "false"})
        return [item for item in data.get("results") or [] if isinstance(item, dict)]

    def pipelines(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/crm/v3/pipelines/deals", query={"archived": "false"})
        return [item for item in data.get("results") or [] if isinstance(item, dict)]

    def _company_properties(self) -> list[str]:
        names = list(COMPANY_PROPERTIES)
        for name in self.extra_company_properties:
            if name and name not in names:
                names.append(name)
        return names

    def search_companies(self, property_name: str, value: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if property_name not in {"domain", "name"}:
            raise ValueError("Only domain and name identity searches are supported")
        operator = "EQ" if property_name == "domain" else "CONTAINS_TOKEN"
        data = self.request(
            "POST",
            "/crm/v3/objects/companies/search",
            body={
                "filterGroups": [{"filters": [{"propertyName": property_name, "operator": operator, "value": value}]}],
                "properties": self._company_properties(),
                "limit": min(max(1, limit), 100),
            },
        )
        return [item for item in data.get("results") or [] if isinstance(item, dict)]

    def association_ids(self, from_type: str, from_id: str, to_type: str) -> list[str]:
        results: list[str] = []
        after: str | None = None
        seen_after: set[str] = set()
        page_limit = min(max(1, self.settings.hubspot_association_page_limit), 500)
        for _page in range(max(1, self.settings.hubspot_association_page_guard)):
            query: dict[str, Any] = {"limit": page_limit}
            if after:
                query["after"] = after
            data = self.request(
                "GET",
                f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}",
                query=query,
            )
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                value = item.get("toObjectId", item.get("id"))
                if value is not None:
                    results.append(str(value))
            next_page = ((data.get("paging") or {}).get("next") or {})
            next_after = next_page.get("after")
            if next_after is None:
                return list(dict.fromkeys(results))
            after = str(next_after)
            if after in seen_after:
                raise HubSpotReadError("Association pagination cursor repeated", category="pagination_error")
            seen_after.add(after)
        raise HubSpotReadError("Association page guard exhausted", category="pagination_guard")

    def batch_read(
        self,
        object_type: str,
        object_ids: Iterable[str],
        properties: Iterable[str],
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in object_ids if value))
        results: list[dict[str, Any]] = []
        size = min(max(1, self.settings.hubspot_batch_read_size), 100)
        for offset in range(0, len(ids), size):
            chunk = ids[offset : offset + size]
            data = self.request(
                "POST",
                f"/crm/v3/objects/{object_type}/batch/read",
                body={
                    "properties": list(properties),
                    "inputs": [{"id": value} for value in chunk],
                },
            )
            results.extend(item for item in data.get("results") or [] if isinstance(item, dict))
            errors = data.get("errors") or []
            if errors:
                raise HubSpotReadError(
                    f"HubSpot {object_type} batch returned {len(errors)} item errors",
                    category="batch_item_error",
                )
        return results

    def batch_association_ids(
        self,
        from_type: str,
        from_ids: Iterable[str],
        to_type: str,
    ) -> dict[str, list[str]]:
        """Read associations for many records without parallel requests.

        HubSpot returns HTTP 207 and OBJECT_NOT_FOUND rows when an input simply
        has no associations. Those rows are an expected empty result, not a
        failed lookup. Any other item error fails closed.
        """
        ids = list(dict.fromkeys(str(value) for value in from_ids if value))
        mapping: dict[str, list[str]] = {value: [] for value in ids}
        size = min(max(1, self.settings.hubspot_batch_read_size), 100)
        for offset in range(0, len(ids), size):
            chunk = ids[offset : offset + size]
            data = self.request(
                "POST",
                f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
                body={"inputs": [{"id": value} for value in chunk]},
            )
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("paging"):
                    raise HubSpotReadError(
                        f"HubSpot {from_type}->{to_type} batch association result "
                        "was paginated; refusing incomplete evidence",
                        category="association_batch_pagination",
                    )
                source_id = str((item.get("from") or {}).get("id") or "")
                values = [
                    str(target.get("toObjectId"))
                    for target in item.get("to") or []
                    if isinstance(target, dict) and target.get("toObjectId") is not None
                ]
                if source_id:
                    mapping[source_id] = list(dict.fromkeys(values))
            unexpected = [
                item
                for item in data.get("errors") or []
                if "NO_ASSOCIATIONS_FOUND"
                not in str((item or {}).get("subCategory") or "")
            ]
            if unexpected:
                raise HubSpotReadError(
                    f"HubSpot {from_type}->{to_type} batch returned "
                    f"{len(unexpected)} unexpected item errors",
                    category="association_batch_item_error",
                )
        return mapping

    def read_deals(self, deal_ids: Iterable[str]) -> list[dict[str, Any]]:
        return self.batch_read("deals", deal_ids, DEAL_PROPERTIES)

    def read_contacts(self, contact_ids: Iterable[str]) -> list[dict[str, Any]]:
        return self.batch_read("contacts", contact_ids, CONTACT_PROPERTIES)

    def list_memberships(self, object_type_id: str, record_id: str) -> set[str]:
        data = self.request(
            "GET",
            f"/crm/v3/lists/records/{object_type_id}/{record_id}/memberships",
        )
        found: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in {"listid", "list_id", "ilslistid", "listids"}:
                        if isinstance(item, list):
                            found.update(str(entry) for entry in item)
                        elif item is not None:
                            found.add(str(item))
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(data)
        return found

