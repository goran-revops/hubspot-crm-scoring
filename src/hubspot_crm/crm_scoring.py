from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from hubspot_crm import db
from hubspot_crm.config import Settings
from hubspot_crm.identity import load_list_ids as load_dedupe_list_ids, normalize_company_name
from hubspot_crm.hubspot_client import (
    HubSpotClient,
    HubSpotReadError,
    RequestCapExceeded,
)
from hubspot_crm.hubspot_engagement import parse_hubspot_datetime
from hubspot_crm.models import CRMAccountInput, CRMScoreResult

DISPOSITIONS = ("hard_skip", "recycle", "net_new", "manual_review")
GENERIC_EMAIL_PREFIXES = {"info", "contact", "support", "sales", "admin", "office"}
CRM_CACHE_SCHEMA_VERSION = 3


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _domain(value: str) -> str:
    text = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.split("/", 1)[0].removeprefix("www.").strip(".")


def _domain_label(value: str) -> str:
    normalized = _domain(value)
    parts = normalized.split(".")
    return parts[-2] if len(parts) >= 2 else normalized


def _source_alternate_domains(account: CRMAccountInput) -> tuple[list[str], list[str]]:
    primary = _domain(account.domain)
    values = account.source_payload.get("other_domains") or []
    if not isinstance(values, list):
        values = []
    alternates = list(
        dict.fromkeys(
            normalized
            for value in values
            if (normalized := _domain(str(value))) and normalized != primary
        )
    )
    primary_label = _domain_label(primary)
    same_label = [
        value for value in alternates if _domain_label(value) == primary_label
    ]
    other = [value for value in alternates if value not in same_label]
    return same_label, other


def load_semantics(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("HubSpot semantics config must be a JSON object")
    return value


def _prop(record: dict[str, Any], key: str) -> str:
    props = record.get("properties") or {}
    return str(props.get(key) or "")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _latest_date(
    record: dict[str, Any],
    keys: Iterable[str],
    *,
    exclude_creation_alias: bool = False,
) -> tuple[datetime | None, str, bool]:
    best: datetime | None = None
    source = ""
    creation_only_ignored = False
    props = record.get("properties") or {}
    created_at = parse_hubspot_datetime(props.get("createdate"))
    for key in keys:
        parsed = parse_hubspot_datetime(props.get(key))
        if (
            exclude_creation_alias
            and key == "notes_last_updated"
            and parsed is not None
            and created_at is not None
            and abs((parsed - created_at).total_seconds()) <= 60
        ):
            creation_only_ignored = True
            continue
        if parsed is not None and (best is None or parsed > best):
            best = parsed
            source = key
    return best, source, creation_only_ignored


def _record_source(record: dict[str, Any]) -> tuple[str, str]:
    source = (
        _prop(record, "hs_object_source_label")
        or _prop(record, "hs_object_source")
        or _prop(record, "hs_analytics_source")
    )
    details = [
        _prop(record, key)
        for key in (
            "hs_object_source_detail_1",
            "hs_object_source_detail_2",
            "hs_object_source_detail_3",
            "hs_analytics_source_data_1",
            "hs_analytics_source_data_2",
        )
    ]
    return source, " | ".join(dict.fromkeys(value for value in details if value))


def _contact_roster(contacts: list[dict[str, Any]], *, limit: int = 10) -> str:
    rows = []
    for contact in contacts[:limit]:
        props = contact.get("properties") or {}
        source, source_details = _record_source(contact)
        emails = [
            str(props.get("email") or "").strip().lower(),
            str(props.get("work_email") or "").strip().lower(),
        ]
        emails.extend(
            value.strip().lower()
            for value in str(props.get("hs_additional_emails") or "").replace(",", ";").split(";")
        )
        rows.append(
            {
                "contact_id": str(contact.get("id") or ""),
                "name": " ".join(
                    value
                    for value in (
                        str(props.get("firstname") or "").strip(),
                        str(props.get("lastname") or "").strip(),
                    )
                    if value
                ),
                "title": str(props.get("jobtitle") or ""),
                "emails": sorted(
                    set(value for value in emails if "@" in value and len(value) <= 254)
                ),
                "created_at": str(props.get("createdate") or ""),
                "record_source": source,
                "record_source_details": source_details,
                "lifecycle_stage": str(props.get("lifecyclestage") or ""),
                "last_contacted": str(props.get("notes_last_contacted") or ""),
                "last_activity": str(props.get("notes_last_updated") or ""),
                "last_sales_activity_type": str(
                    props.get("hs_last_sales_activity_type") or ""
                ),
            }
        )
    return json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _candidate_summary(records: list[dict[str, Any]]) -> str:
    values = []
    for record in records[:10]:
        values.append(
            ":".join(
                [
                    str(record.get("id") or ""),
                    _domain(_prop(record, "domain")),
                    normalize_company_name(_prop(record, "name")),
                ]
            )
        )
    return ";".join(values)


def _choose_company(
    account: CRMAccountInput,
    domain_results: list[dict[str, Any]],
    name_results: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, str, list[dict[str, Any]], str]:
    target_domain = _domain(account.domain)
    exact_domain = [
        item for item in domain_results if _domain(_prop(item, "domain")) == target_domain
    ]
    if exact_domain:
        ordered = sorted(exact_domain, key=lambda item: str(item.get("id") or ""))
        if len(ordered) > 1:
            return ordered[0], "exact_domain", "ambiguous", ordered, "multiple_exact_domain_matches"
        return ordered[0], "exact_domain", "high", ordered, ""

    target_name = normalize_company_name(account.account_name)
    exact_name = [
        item
        for item in name_results
        if normalize_company_name(_prop(item, "name")) == target_name and target_name
    ]
    ordered_names = sorted(
        name_results,
        key=lambda item: (
            normalize_company_name(_prop(item, "name")) != target_name,
            _domain(_prop(item, "domain")),
            str(item.get("id") or ""),
        ),
    )
    if ordered_names:
        return (
            None,
            "name_only_candidate",
            "ambiguous",
            ordered_names,
            "name_only_match_rejected_domain_is_authority",
        )
    return None, "no_match", "none", [], (
        "missing_domain_prevents_safe_net_new" if not target_domain else ""
    )


def _deal_state(deal: dict[str, Any], stages: dict[str, Any]) -> str:
    props = deal.get("properties") or {}
    stage = str(props.get("dealstage") or "")
    mapped = stages.get(stage)
    if isinstance(mapped, dict):
        mapped = mapped.get("state")
    if mapped in {"open", "closed_won", "closed_lost"}:
        return str(mapped)
    if _truthy(props.get("hs_is_closed_won")):
        return "closed_won"
    if _truthy(props.get("hs_is_closed_lost")):
        return "closed_lost"
    closed = str(props.get("hs_is_closed") or "").strip().lower()
    if closed == "false":
        return "open"
    if closed == "true":
        return "unknown"
    return "unknown"


def _date_value(record: dict[str, Any], preferred: tuple[str, ...]) -> tuple[datetime | None, str]:
    props = record.get("properties") or {}
    for key in preferred:
        parsed = parse_hubspot_datetime(props.get(key))
        if parsed is not None:
            return parsed, str(props.get(key) or "")
    return None, ""


def _summarize_deal_history(
    deals: list[dict[str, Any]],
    semantics: dict[str, Any],
) -> dict[str, Any]:
    stages = semantics.get("deal_stages") or {}
    open_deals: list[tuple[datetime, dict[str, Any]]] = []
    won: list[tuple[datetime, dict[str, Any]]] = []
    lost: list[tuple[datetime, dict[str, Any]]] = []
    unknown: list[str] = []
    for deal in deals:
        state = _deal_state(deal, stages)
        date, _raw = _date_value(
            deal,
            ("closedate", "createdate") if state != "open" else ("createdate", "closedate"),
        )
        dated = (date or datetime.min.replace(tzinfo=UTC), deal)
        if state == "open":
            open_deals.append(dated)
        elif state == "closed_won":
            won.append(dated)
        elif state == "closed_lost":
            lost.append(dated)
        else:
            unknown.append(f"{deal.get('id', '')}:{_prop(deal, 'pipeline')}:{_prop(deal, 'dealstage')}")
    open_deals.sort(key=lambda item: item[0], reverse=True)
    won.sort(key=lambda item: item[0], reverse=True)
    lost.sort(key=lambda item: item[0], reverse=True)
    latest_open = open_deals[0][1] if open_deals else {}
    latest_won = won[0][1] if won else {}
    latest_lost = lost[0][1] if lost else {}
    open_date, _ = _date_value(latest_open, ("createdate", "closedate"))
    won_date, _ = _date_value(latest_won, ("closedate", "createdate"))
    lost_date, _ = _date_value(latest_lost, ("closedate", "createdate"))
    return {
        "associated_deal_count": len(deals),
        "open_deal_count": len(open_deals),
        "latest_open_deal_id": str(latest_open.get("id") or ""),
        "latest_open_deal_stage": _prop(latest_open, "dealstage"),
        "latest_open_deal_date": open_date.date().isoformat() if open_date else "",
        "latest_open_deal_amount": _prop(latest_open, "amount"),
        "closed_won_count": len(won),
        "latest_closed_won_date": won_date.date().isoformat() if won_date else "",
        "closed_lost_count": len(lost),
        "latest_closed_lost_date": lost_date.date().isoformat() if lost_date else "",
        "latest_closed_lost_stage": _prop(latest_lost, "dealstage"),
        "latest_closed_lost_amount": _prop(latest_lost, "amount"),
        "deal_conflict": ";".join(unknown[:10]),
    }


def _contact_evidence(
    company: dict[str, Any],
    contacts: list[dict[str, Any]],
    semantics: dict[str, Any],
) -> dict[str, Any]:
    activity = semantics.get("meaningful_activity_properties") or {}
    company_keys = activity.get("companies") or []
    contact_keys = activity.get("contacts") or []
    company_date, company_source, company_creation_ignored = _latest_date(
        company,
        company_keys,
        exclude_creation_alias=True,
    )
    best = company_date
    best_type = "company_activity" if company_date else ""
    best_source = f"company.{company_source}" if company_source else ""
    creation_only_ignored = company_creation_ignored
    emails: set[str] = set()
    for contact in contacts:
        props = contact.get("properties") or {}
        raw_emails = [str(props.get("email") or ""), str(props.get("work_email") or "")]
        raw_emails.extend(
            value.strip()
            for value in str(props.get("hs_additional_emails") or "").replace(",", ";").split(";")
        )
        for email in raw_emails:
            lowered = email.strip().lower()
            if "@" in lowered and len(lowered) <= 254:
                emails.add(lowered)
        contact_date, contact_source, contact_creation_ignored = _latest_date(
            contact,
            contact_keys,
            exclude_creation_alias=True,
        )
        creation_only_ignored = creation_only_ignored or contact_creation_ignored
        if contact_date is not None and (best is None or contact_date > best):
            best = contact_date
            best_type = "contact_activity"
            best_source = f"contact.{contact_source}"
    return {
        "associated_contact_count": len(contacts),
        "business_email_count": len(emails),
        "business_email_summary": ";".join(sorted(emails)[:25]),
        "contact_roster_summary": _contact_roster(contacts),
        "contact_evidence_coverage": (
            f"all_associated_contacts_read={len(contacts)};"
            f"roster_rows_displayed={min(len(contacts), 10)};"
            "all_business_emails_counted=true"
        ),
        "record_creation_only_activity_ignored": creation_only_ignored,
        "latest_activity_date": best.date().isoformat() if best else "",
        "latest_activity_type": best_type,
        "latest_activity_source": best_source,
        "_latest_activity_datetime": best,
    }


class CRMScorer:
    def __init__(self, settings: Settings, client: HubSpotClient) -> None:
        self.settings = settings
        self.client = client
        self.semantics = load_semantics(settings.hubspot_semantics_path)
        self.configured_list_ids = load_dedupe_list_ids(self.settings.hubspot_lists_path)
        extras: list[str] = []
        status_field = str(self.semantics.get("account_status_property") or "")
        if status_field:
            extras.append(status_field)
        custom = self.semantics.get("customer_status_fields") or {}
        if isinstance(custom, dict):
            extras.extend(str(name) for name in custom)
        self.client.extra_company_properties = extras

    def score(self, account: CRMAccountInput) -> CRMScoreResult:
        scored_at = _iso(_now())
        base = CRMScoreResult(
            canonical_key=account.canonical_key,
            account_name=account.account_name,
            domain=_domain(account.domain),
            source=account.source,
            cohort=account.cohort,
            source_record_id=account.source_record_id,
            provenance=account.provenance,
            scored_at=scored_at,
        )
        try:
            primary_domain_results = (
                self.client.search_companies("domain", _domain(account.domain), limit=100)
                if _domain(account.domain)
                else []
            )
            domain_results = list(primary_domain_results)
            alternate_basis = ""
            alternate_error = ""
            if not primary_domain_results and _domain(account.domain):
                same_label_domains, other_alias_domains = _source_alternate_domains(account)
                for group, basis in (
                    (same_label_domains, "exact_alternate_domain"),
                    (other_alias_domains, "exact_source_alias_domain"),
                ):
                    matches: list[dict[str, Any]] = []
                    for alternate_domain in group:
                        matches.extend(
                            self.client.search_companies(
                                "domain",
                                alternate_domain,
                                limit=100,
                            )
                        )
                    unique_matches = {
                        str(item.get("id") or ""): item
                        for item in matches
                        if item.get("id")
                    }
                    if len(unique_matches) == 1:
                        domain_results = list(unique_matches.values())
                        alternate_basis = basis
                        break
                    if len(unique_matches) > 1:
                        domain_results = list(unique_matches.values())
                        alternate_error = "multiple_source_alias_domain_matches"
                        break
            name_results = []
            if not domain_results and account.account_name:
                name_results = self.client.search_companies("name", account.account_name, limit=100)
            if alternate_error:
                company = None
                basis = "ambiguous_source_alias_domains"
                confidence = "ambiguous"
                candidates = domain_results
                identity_error = alternate_error
            elif alternate_basis and len(domain_results) == 1:
                company = domain_results[0]
                basis = alternate_basis
                confidence = "high"
                candidates = domain_results
                identity_error = ""
            else:
                company, basis, confidence, candidates, identity_error = _choose_company(
                    account, domain_results, name_results
                )
            base = replace(
                base,
                company_found=bool(company),
                company_id=str((company or {}).get("id") or ""),
                company_name=_prop(company or {}, "name"),
                company_domain=_domain(_prop(company or {}, "domain")),
                company_created_at=_prop(company or {}, "createdate"),
                company_record_source=_record_source(company or {})[0],
                company_record_source_details=_record_source(company or {})[1],
                lifecycle_stage=_prop(company or {}, "lifecyclestage"),
                owner_id=_prop(company or {}, "hubspot_owner_id"),
                match_basis=basis,
                match_confidence=confidence,
                match_candidate_count=len(candidates),
                match_candidates_summary=_candidate_summary(candidates),
            )
            if not company:
                if identity_error:
                    behavior = str(
                        self.semantics.get(
                            "identity_error_behavior",
                            "manual_review",
                        )
                    )
                    return replace(
                        base,
                        tier="hard_skip" if behavior == "hard_skip" else "manual_review",
                        reason=(
                            "conservative_identity_suppression"
                            if behavior == "hard_skip"
                            else "identity_requires_manual_review"
                        ),
                        lookup_error=identity_error,
                        evidence=identity_error,
                    )
                return replace(
                    base,
                    tier="net_new",
                    reason="no_hubspot_company_match",
                    evidence="domain_search_complete:no_match",
                )
            return self._score_company(base, company, identity_error)
        except RequestCapExceeded:
            raise
        except Exception as exc:
            category = exc.category if isinstance(exc, HubSpotReadError) else type(exc).__name__
            behavior = str(
                self.semantics.get("lookup_error_behavior", "manual_review")
            )
            return replace(
                base,
                tier="hard_skip" if behavior == "hard_skip" else "manual_review",
                reason=(
                    "conservative_lookup_failure_suppression"
                    if behavior == "hard_skip"
                    else "hubspot_lookup_error"
                ),
                lookup_error=f"{category}:{str(exc)[:500]}",
                evidence=f"lookup_failed:{category}",
            )

    def _score_company(
        self,
        base: CRMScoreResult,
        company: dict[str, Any],
        identity_error: str,
    ) -> CRMScoreResult:
        company_id = str(company.get("id") or "")
        errors: list[str] = []
        try:
            memberships = self.client.list_memberships("0-2", company_id)
            if not self.configured_list_ids:
                membership_status = "configuration_empty"
            elif memberships & self.configured_list_ids:
                membership_status = "configured_member"
            else:
                membership_status = "not_configured_member"
            membership_error = ""
        except RequestCapExceeded:
            raise
        except Exception as exc:
            memberships = set()
            membership_status = "lookup_failed"
            membership_error = str(exc)[:500]
            errors.append("list_membership_lookup_failed")

        try:
            deal_ids = self.client.association_ids("companies", company_id, "deals")
            deals = self.client.read_deals(deal_ids)
            if len(deals) != len(set(deal_ids)):
                errors.append("deal_batch_incomplete")
            deal_summary = _summarize_deal_history(deals, self.semantics)
        except RequestCapExceeded:
            raise
        except Exception as exc:
            deal_ids = []
            deals = []
            deal_summary = _summarize_deal_history([], self.semantics)
            errors.append(f"deal_lookup_failed:{str(exc)[:300]}")

        contact_lookup_failed = False
        try:
            contact_ids = self.client.association_ids("companies", company_id, "contacts")
            contacts = self.client.read_contacts(contact_ids)
            if len(contacts) != len(set(contact_ids)):
                errors.append("contact_batch_incomplete")
        except RequestCapExceeded:
            raise
        except Exception as exc:
            contact_ids = []
            contacts = []
            contact_lookup_failed = True
            errors.append(f"contact_lookup_failed:{str(exc)[:300]}")
        contact_summary = _contact_evidence(company, contacts, self.semantics)

        account_status_property = str(self.semantics.get("account_status_property") or "")
        account_status = _prop(company, account_status_property) if account_status_property else ""
        lifecycle = _prop(company, "lifecyclestage").lower()
        customer_values = {
            str(value).lower() for value in self.semantics.get("customer_lifecycle_values") or []
        }
        inactive_values = {
            str(value).lower() for value in self.semantics.get("inactive_status_values") or []
        }
        customer_status_fields = self.semantics.get("customer_status_fields") or {}
        customer_field_matches = [
            f"{field}={_prop(company, field)}"
            for field, values in customer_status_fields.items()
            if _prop(company, field).lower() in {str(value).lower() for value in values}
        ]
        is_customer = lifecycle in customer_values or bool(customer_field_matches)
        is_inactive = bool(account_status and account_status.lower() in inactive_values)
        latest_activity = contact_summary.pop("_latest_activity_datetime")
        is_recent = bool(
            latest_activity
            and (_now() - latest_activity).total_seconds()
            <= max(0, self.settings.hubspot_recent_activity_days) * 86400
        )

        tier = "recycle"
        reason = "existing_company_stale"
        conflicts: list[str] = []
        if identity_error:
            conflicts.append(identity_error)
        if errors:
            conflicts.extend(errors)
        if deal_summary["deal_conflict"]:
            conflicts.append("unknown_deal_stage")
        if is_inactive:
            conflicts.append("inactive_or_stub_status")
        if membership_status == "configured_member":
            behavior = self.semantics.get("configured_list_member_behavior", "manual_review")
            if behavior == "hard_skip":
                tier, reason = "hard_skip", "configured_list_member"
            elif behavior == "manual_review":
                conflicts.append("configured_list_membership_requires_review")
        if deal_summary["closed_won_count"] and not is_customer:
            behavior = self.semantics.get("closed_won_without_customer_behavior", "manual_review")
            if behavior == "hard_skip":
                tier, reason = "hard_skip", "closed_won_history"
            elif behavior == "manual_review":
                conflicts.append("closed_won_without_customer_status")
        if is_customer:
            tier, reason = "hard_skip", "current_customer_lifecycle"
        elif deal_summary["open_deal_count"]:
            tier, reason = "hard_skip", "open_associated_deal"
        elif is_recent:
            tier, reason = "hard_skip", "recent_meaningful_activity"
        elif deal_summary["closed_lost_count"]:
            tier, reason = "recycle", "existing_company_old_closed_lost"
        if conflicts and tier != "hard_skip":
            behavior = str(
                self.semantics.get(
                    "unresolved_evidence_behavior",
                    "manual_review",
                )
            )
            if behavior == "hard_skip":
                tier, reason = "hard_skip", "conservative_unresolved_evidence_suppression"
            else:
                tier, reason = "manual_review", "conflicting_or_incomplete_crm_evidence"

        evidence = "|".join(
            part
            for part in [
                f"lifecycle={lifecycle}",
                f"customer_status={';'.join(customer_field_matches)}" if customer_field_matches else "",
                f"account_status={account_status}" if account_status else "",
                f"contacts={len(contacts)}",
                f"emails={contact_summary['business_email_count']}",
                f"deals={deal_summary['associated_deal_count']}",
                f"open={deal_summary['open_deal_count']}",
                f"won={deal_summary['closed_won_count']}",
                f"lost={deal_summary['closed_lost_count']}",
                f"activity={contact_summary['latest_activity_source']}:{contact_summary['latest_activity_date']}"
                if contact_summary["latest_activity_date"]
                else "",
                "record_creation_activity_ignored=true"
                if contact_summary["record_creation_only_activity_ignored"]
                else "",
                f"list={membership_status}",
                f"conflicts={';'.join(conflicts)}" if conflicts else "",
            ]
            if part
        )
        return replace(
            base,
            account_status=account_status,
            list_membership_status=membership_status,
            list_membership_ids=";".join(sorted(memberships & self.configured_list_ids)),
            list_membership_error=membership_error,
            lookup_error=" | ".join(errors),
            tier=tier,
            reason=reason,
            evidence=evidence,
            **contact_summary,
            **deal_summary,
        )


def accounts_fingerprint(accounts: Iterable[CRMAccountInput]) -> str:
    canonical = [
        {
            "canonical_key": account.canonical_key,
            "source": account.source,
            "cohort": account.cohort,
            "source_record_id": account.source_record_id,
            "provenance": account.provenance,
        }
        for account in accounts
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_canonical_accounts(path: Path) -> list[CRMAccountInput]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif suffix == ".json":
        parsed = json.loads(path.read_text(encoding="utf-8"))
        values = parsed if isinstance(parsed, list) else parsed.get("accounts") or []
    elif suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            values = list(csv.DictReader(handle))
    else:
        raise ValueError("Canonical CRM input must be .csv, .json, or .jsonl")
    accounts = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        name = str(value.get("account_name") or value.get("company_name") or value.get("organization_name") or "")
        domain = str(value.get("domain") or value.get("company_domain") or "")
        accounts.append(
            CRMAccountInput(
                account_name=name,
                domain=domain,
                source=str(value.get("source") or ""),
                cohort=str(value.get("cohort") or value.get("saved_search") or ""),
                source_record_id=str(value.get("source_record_id") or index),
                provenance=str(value.get("provenance") or path.name),
                source_payload={key: item for key, item in value.items() if key not in {"email", "token"}},
            )
        )
    return accounts


def _cache_result_for_account(cached: dict[str, Any], account: CRMAccountInput) -> CRMScoreResult:
    allowed = CRMScoreResult.__dataclass_fields__
    values = {key: value for key, value in cached.items() if key in allowed}
    values.update(
        {
            "canonical_key": account.canonical_key,
            "account_name": account.account_name,
            "domain": _domain(account.domain),
            "source": account.source,
            "cohort": account.cohort,
            "source_record_id": account.source_record_id,
            "provenance": account.provenance,
            "cache_status": "hit",
        }
    )
    return CRMScoreResult(**values)


def _valid_cache_payload(cached_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not cached_row:
        return None
    try:
        payload = json.loads(str(cached_row.get("result_json") or "{}"))
    except (TypeError, ValueError):
        return None
    if payload.get("_cache_schema_version") != CRM_CACHE_SCHEMA_VERSION:
        return None
    return payload


SOURCE_CONTEXT_COLUMNS = ["source_payload_json"]


def _source_context(account: CRMAccountInput) -> dict[str, Any]:
    payload = account.source_payload or {}
    return {
        "source_payload_json": json.dumps(payload, sort_keys=True, ensure_ascii=True),
    }


def _write_results(
    output_dir: Path,
    results: list[CRMScoreResult],
    summary: dict[str, Any],
    accounts: list[CRMAccountInput],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {**result.as_dict(), **_source_context(account)}
        for result, account in zip(results, accounts, strict=True)
    ]
    columns = list(CRMScoreResult.__dataclass_fields__) + SOURCE_CONTEXT_COLUMNS
    temp_csv = output_dir / "crm_account_scores.csv.tmp"
    with temp_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    temp_csv.replace(output_dir / "crm_account_scores.csv")
    temp_jsonl = output_dir / "crm_account_scores.jsonl.tmp"
    temp_jsonl.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    temp_jsonl.replace(output_dir / "crm_account_scores.jsonl")
    temp_summary = output_dir / "crm_score_summary.json.tmp"
    temp_summary.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    temp_summary.replace(output_dir / "crm_score_summary.json")


def plan_crm_batch(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list[CRMAccountInput],
) -> dict[str, Any]:
    unique = list(dict.fromkeys(account.canonical_key for account in accounts))
    cache_hits = sum(
        bool(_valid_cache_payload(db.get_crm_cache(conn, key)))
        for key in unique
    )
    misses = len(unique) - cache_hits
    return {
        "mode": "plan",
        "accounts": len(accounts),
        "unique_accounts": len(unique),
        "input_fingerprint": accounts_fingerprint(accounts),
        "cache_hits": cache_hits,
        "cache_misses": misses,
        "estimated_requests_min": misses,
        "estimated_requests_max": misses * 8,
        "session_request_cap": settings.hubspot_request_cap,
        "recommended_batch_size": settings.hubspot_checkpoint_batch_size,
        "live_requests_made": 0,
        "isolation": {
            "hubspot_only": True,
            "hubspot_writeback": False,
        },
    }


def run_crm_batch(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list[CRMAccountInput],
    *,
    output_dir: Path,
    limit: int | None = None,
    resume: bool = False,
    client: HubSpotClient | None = None,
    interrupt_after: int | None = None,
) -> dict[str, Any]:
    selected = accounts[: max(0, limit)] if limit is not None else accounts
    fingerprint = accounts_fingerprint(selected)
    previous = db.latest_resumable_crm_batch(conn, fingerprint) if resume else None
    start_index = int(previous.get("checkpoint_index") or 0) if previous else 0
    batch_id = str(previous.get("id")) if previous else uuid.uuid4().hex
    if previous:
        db.update_crm_batch(conn, batch_id, status="running", error_message="")
    else:
        db.create_crm_batch(
            conn,
            batch_id=batch_id,
            input_fingerprint=fingerprint,
            source=selected[0].source if selected else "",
            cohort=selected[0].cohort if selected else "",
            total_accounts=len(selected),
            output_dir=str(output_dir),
        )
    db.acquire_crm_batch_lock(conn, fingerprint, batch_id)
    conn.commit()
    scorer_client = client or HubSpotClient(settings)
    scorer = CRMScorer(settings, scorer_client)
    results: list[CRMScoreResult] = []
    cache_hits = 0
    errors = 0
    try:
        for index, account in enumerate(selected):
            if interrupt_after is not None and len(results) >= interrupt_after:
                raise InterruptedError("Simulated bounded interruption")
            cached_payload = _valid_cache_payload(
                db.get_crm_cache(conn, account.canonical_key)
            )
            if cached_payload:
                result = _cache_result_for_account(cached_payload, account)
                cache_hits += 1
            elif index < start_index:
                rows = db.fetch_crm_scores(conn, batch_source=account.source, cohort=account.cohort)
                existing = next(
                    (
                        row for row in rows
                        if row["canonical_key"] == account.canonical_key
                        and row["source_record_id"] == account.source_record_id
                    ),
                    None,
                )
                if not existing:
                    result = scorer.score(account)
                else:
                    allowed = CRMScoreResult.__dataclass_fields__
                    result = CRMScoreResult(**{key: existing[key] for key in allowed})
            else:
                result = scorer.score(account)
                fetched_at = _iso(_now())
                expires_at = _iso(_now() + timedelta(seconds=max(0, settings.hubspot_cache_ttl_seconds)))
                result = replace(result, cache_fetched_at=fetched_at)
                cached_result = result.as_dict()
                cached_result["_cache_schema_version"] = CRM_CACHE_SCHEMA_VERSION
                db.upsert_crm_cache(
                    conn,
                    cache_key=account.canonical_key,
                    company_id=result.company_id,
                    result_json=json.dumps(cached_result, sort_keys=True, ensure_ascii=True),
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                )
                if result.company_id:
                    db.upsert_crm_cache(
                        conn,
                        cache_key=f"company:{result.company_id}",
                        company_id=result.company_id,
                        result_json=json.dumps(cached_result, sort_keys=True, ensure_ascii=True),
                        fetched_at=fetched_at,
                        expires_at=expires_at,
                    )
            if result.tier == "manual_review" and result.lookup_error:
                errors += 1
            db.upsert_crm_score(conn, result.as_dict(), source_payload=account.source_payload)
            results.append(result)
            processed = index + 1
            if processed % max(1, settings.hubspot_checkpoint_batch_size) == 0:
                db.update_crm_batch(
                    conn,
                    batch_id,
                    processed_accounts=processed,
                    checkpoint_index=processed,
                    request_count=scorer_client.metrics.requests,
                    cache_hits=cache_hits,
                    error_count=errors,
                )
                conn.commit()
        counts = {tier: sum(result.tier == tier for result in results) for tier in DISPOSITIONS}
        summary = {
            "batch_id": batch_id,
            "input_fingerprint": fingerprint,
            "accounts": len(selected),
            "processed": len(results),
            "resumed_from_index": start_index,
            "dispositions": counts,
            "cache_hits": cache_hits,
            "lookup_error_results": errors,
            "request_metrics": scorer_client.metrics.as_dict(),
            "output_dir": str(output_dir),
            "isolation": {
                "hubspot_only": True,
                "hubspot_writeback": False,
            },
        }
        _write_results(output_dir, results, summary, selected)
        db.update_crm_batch(
            conn,
            batch_id,
            status="succeeded",
            processed_accounts=len(results),
            checkpoint_index=len(results),
            request_count=scorer_client.metrics.requests,
            cache_hits=cache_hits,
            error_count=errors,
        )
        conn.commit()
        return summary
    except Exception as exc:
        db.update_crm_batch(
            conn,
            batch_id,
            status="failed",
            processed_accounts=len(results),
            checkpoint_index=len(results),
            request_count=scorer_client.metrics.requests,
            cache_hits=cache_hits,
            error_count=errors + 1,
            error_message=str(exc)[:1000],
        )
        conn.commit()
        raise
    finally:
        db.release_crm_batch_lock(conn, fingerprint, batch_id)
        conn.commit()

