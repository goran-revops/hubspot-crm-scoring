from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CRMAccountInput:
    account_name: str
    domain: str = ""
    source: str = ""
    cohort: str = ""
    source_record_id: str = ""
    provenance: str = ""
    source_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_key(self) -> str:
        domain = self.domain.lower().strip().removeprefix("www.")
        if domain:
            return f"domain:{domain}"
        normalized = " ".join(
            part
            for part in "".join(
                character.lower() if character.isalnum() else " " for character in self.account_name
            ).split()
        )
        return f"name:{normalized}"


@dataclass(frozen=True)
class CRMScoreResult:
    canonical_key: str
    account_name: str
    domain: str
    source: str
    cohort: str
    source_record_id: str
    provenance: str
    company_found: bool = False
    company_id: str = ""
    company_name: str = ""
    company_domain: str = ""
    company_created_at: str = ""
    company_record_source: str = ""
    company_record_source_details: str = ""
    lifecycle_stage: str = ""
    account_status: str = ""
    owner_id: str = ""
    match_basis: str = ""
    match_confidence: str = ""
    match_candidate_count: int = 0
    match_candidates_summary: str = ""
    list_membership_status: str = "not_checked"
    list_membership_ids: str = ""
    list_membership_error: str = ""
    latest_activity_date: str = ""
    latest_activity_type: str = ""
    latest_activity_source: str = ""
    associated_contact_count: int = 0
    business_email_count: int = 0
    business_email_summary: str = ""
    contact_roster_summary: str = ""
    contact_evidence_coverage: str = ""
    record_creation_only_activity_ignored: bool = False
    contact_usage_policy: str = "hubspot_evidence_only"
    associated_deal_count: int = 0
    open_deal_count: int = 0
    latest_open_deal_id: str = ""
    latest_open_deal_stage: str = ""
    latest_open_deal_date: str = ""
    latest_open_deal_amount: str = ""
    closed_won_count: int = 0
    latest_closed_won_date: str = ""
    closed_lost_count: int = 0
    latest_closed_lost_date: str = ""
    latest_closed_lost_stage: str = ""
    latest_closed_lost_amount: str = ""
    deal_conflict: str = ""
    lookup_error: str = ""
    scope_property_errors: str = ""
    tier: str = "manual_review"
    reason: str = ""
    evidence: str = ""
    cache_status: str = "miss"
    cache_fetched_at: str = ""
    scored_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
