from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hubspot_crm.config import Settings
from hubspot_crm.hubspot_client import HubSpotClient
from hubspot_crm.crm_scoring import load_semantics

ACTIVITY_FIELDS = (
    "notes_last_contacted",
    "notes_last_updated",
    "hs_last_sales_activity_timestamp",
    "hs_last_booked_meeting_date",
    "engagements_last_meeting_booked",
    "hs_last_logged_call_date",
    "hs_last_logged_outgoing_email_date",
    "hs_sales_email_last_replied",
    "hs_latest_meeting_activity",
)


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stage_map(pipelines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for pipeline in pipelines:
        pipeline_id = str(pipeline.get("id") or "")
        for stage in pipeline.get("stages") or []:
            stage_id = str(stage.get("id") or "")
            metadata = stage.get("metadata") or {}
            is_closed = str(metadata.get("isClosed") or "").lower() == "true"
            probability = str(metadata.get("probability") or "")
            if not is_closed:
                state = "open"
            elif probability in {"1", "1.0", "100", "100.0"}:
                state = "closed_won"
            elif probability in {"0", "0.0"}:
                state = "closed_lost"
            else:
                state = "unknown"
            mapping[stage_id] = {
                "state": state,
                "pipeline_id": pipeline_id,
                "pipeline_label": str(pipeline.get("label") or ""),
                "stage_label": str(stage.get("label") or ""),
                "archived": bool(stage.get("archived")),
                "source": "HubSpot pipeline metadata",
            }
    return mapping


def discover_portal(
    settings: Settings,
    *,
    client: HubSpotClient | None = None,
) -> dict[str, Any]:
    """Read pipeline stages and customer fields. Write semantics. No CRM writes."""
    hubspot = client or HubSpotClient(settings, request_cap=min(settings.hubspot_request_cap, 100))
    company_defs = hubspot.property_definitions("companies")
    contact_defs = hubspot.property_definitions("contacts")
    pipelines = hubspot.pipelines()
    company_defs_by_name = {str(item.get("name") or ""): item for item in company_defs}
    contact_names = {str(item.get("name") or "") for item in contact_defs}

    semantics = load_semantics(settings.hubspot_semantics_path)
    semantics["verified_at"] = _timestamp()
    semantics["deal_stages"] = _stage_map(pipelines)
    existing = semantics.get("customer_status_fields") or {}
    customer_status_fields = dict(existing) if isinstance(existing, dict) else {}
    if "hs_current_customer" in company_defs_by_name:
        customer_status_fields["hs_current_customer"] = [
            str(option.get("value") or "")
            for option in company_defs_by_name["hs_current_customer"].get("options") or []
            if str(option.get("label") or "").strip().lower() == "yes"
        ]
    semantics["customer_status_fields"] = customer_status_fields
    semantics["meaningful_activity_properties"] = {
        "companies": [name for name in ACTIVITY_FIELDS if name in company_defs_by_name],
        "contacts": [name for name in ACTIVITY_FIELDS if name in contact_names],
    }
    settings.hubspot_semantics_path.parent.mkdir(parents=True, exist_ok=True)
    settings.hubspot_semantics_path.write_text(
        json.dumps(semantics, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "deal_stages": len(semantics["deal_stages"]),
        "customer_status_fields": sorted(customer_status_fields),
        "requests": hubspot.metrics.requests,
        "hubspot_writeback": False,
    }
