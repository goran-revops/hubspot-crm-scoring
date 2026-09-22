from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hubspot_crm import db
from hubspot_crm.hubspot_client import (
    HubSpotClient,
    HubSpotReadError,
    RequestCapExceeded,
    RequestMetrics,
)
from hubspot_crm.models import CRMAccountInput, CRMScoreResult
from hubspot_crm.crm_scoring import (
    CRMScorer,
    SOURCE_CONTEXT_COLUMNS,
    plan_crm_batch,
    run_crm_batch,
)


def _semantics(tmp_path: Path, **overrides):
    value = {
        "customer_lifecycle_values": ["customer"],
        "customer_status_fields": {
            "hs_current_customer": ["yes"],
            "example_customer_status": ["Customer"],
        },
        "account_status_property": "account_status",
        "inactive_status_values": ["inactive"],
        "closed_won_without_customer_behavior": "manual_review",
        "configured_list_member_behavior": "manual_review",
        "meaningful_activity_properties": {
            "companies": ["notes_last_contacted"],
            "contacts": ["notes_last_contacted"],
        },
        "deal_stages": {
            "open-stage": {"state": "open"},
            "won-stage": {"state": "closed_won"},
            "lost-stage": {"state": "closed_lost"},
        },
    }
    value.update(overrides)
    path = tmp_path / "semantics.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class FakeScoringClient:
    def __init__(
        self,
        *,
        companies=None,
        deals=None,
        contacts=None,
        memberships=None,
        fail_contacts=False,
        fail_memberships=False,
    ):
        self.companies = companies or []
        self.deals = deals or []
        self.contacts = contacts or []
        self.memberships = memberships or set()
        self.fail_contacts = fail_contacts
        self.fail_memberships = fail_memberships
        self.metrics = RequestMetrics()

    def search_companies(self, property_name, value, limit=100):
        self.metrics.requests += 1
        return self.companies

    def list_memberships(self, object_type, record_id):
        self.metrics.requests += 1
        if self.fail_memberships:
            raise HubSpotReadError("denied", status=403, category="authorization")
        return self.memberships

    def association_ids(self, from_type, from_id, to_type):
        self.metrics.requests += 1
        if to_type == "contacts" and self.fail_contacts:
            raise HubSpotReadError("contact association denied", status=403, category="authorization")
        values = self.contacts if to_type == "contacts" else self.deals
        return [str(value["id"]) for value in values]

    def read_contacts(self, ids):
        self.metrics.requests += 1
        return self.contacts

    def read_deals(self, ids):
        self.metrics.requests += 1
        return self.deals


def _company(**props):
    values = {"name": "Acme", "domain": "acme.example", "lifecyclestage": "lead"}
    values.update(props)
    return {"id": "101", "properties": values}


def _deal(deal_id, stage, **props):
    values = {"dealstage": stage, "createdate": "2020-01-01T00:00:00Z"}
    values.update(props)
    return {"id": deal_id, "properties": values}


def _account(domain="acme.example"):
    return CRMAccountInput(
        account_name="Acme",
        domain=domain,
        source="fixture",
        cohort="test",
        source_record_id=domain or "name-only",
        provenance="fixture",
    )


def test_association_pagination_and_guard(settings):
    pages = []

    def transport(method, url, headers, query, body, timeout):
        pages.append(query.get("after") if query else None)
        if len(pages) == 1:
            return 200, {}, {"results": [{"toObjectId": 1}], "paging": {"next": {"after": "next"}}}
        return 200, {}, {"results": [{"toObjectId": 2}]}

    client = HubSpotClient(settings, transport=transport)
    assert client.association_ids("companies", "1", "deals") == ["1", "2"]
    assert pages == [None, "next"]


def test_batch_associations_treat_no_association_as_empty(settings):
    def transport(method, url, headers, query, body, timeout):
        return 207, {}, {
            "results": [
                {
                    "from": {"id": "1"},
                    "to": [{"toObjectId": 10}, {"toObjectId": 11}],
                }
            ],
            "errors": [
                {
                    "subCategory": "crm.associations.NO_ASSOCIATIONS_FOUND",
                    "context": {"fromObjectId": ["2"]},
                }
            ],
        }

    client = HubSpotClient(settings, transport=transport)
    assert client.batch_association_ids("contacts", ["1", "2"], "meetings") == {
        "1": ["10", "11"],
        "2": [],
    }


def test_read_only_client_blocks_mutating_post(settings):
    client = HubSpotClient(
        settings,
        transport=lambda *_args: pytest.fail("transport must not be called"),
    )
    with pytest.raises(ValueError, match="blocks POST"):
        client.request("POST", "/crm/v3/objects/companies", body={"properties": {}})


def test_deal_and_contact_batch_reads_chunk_at_100(settings):
    calls = []

    def transport(method, url, headers, query, body, timeout):
        calls.append((url, len(body["inputs"])))
        return 200, {}, {"results": [{"id": item["id"], "properties": {}} for item in body["inputs"]]}

    client = HubSpotClient(settings, transport=transport)
    assert len(client.read_deals([str(i) for i in range(205)])) == 205
    assert [size for _url, size in calls] == [100, 100, 5]


def test_retry_429_then_success(settings):
    attempts = []
    sleeps = []

    def transport(method, url, headers, query, body, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise HubSpotReadError("rate limited", status=429, retryable=True)
        return 200, {"X-HubSpot-RateLimit-Remaining": "99"}, {"results": []}

    client = HubSpotClient(settings, transport=transport, sleep=sleeps.append)
    assert client.pipelines() == []
    assert client.metrics.retries == 2
    assert client.metrics.requests == 3
    assert len(sleeps) == 2


def test_retry_after_header_is_honored(settings):
    attempts = []
    sleeps = []

    def transport(method, url, headers, query, body, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise HubSpotReadError(
                "rate limited",
                status=429,
                retryable=True,
                headers={"Retry-After": "2"},
            )
        return 200, {}, {"results": []}

    client = HubSpotClient(settings, transport=transport, sleep=sleeps.append)
    client.pipelines()
    assert sleeps == [2.0]


def test_retry_exhaustion_never_becomes_success(settings):
    def transport(method, url, headers, query, body, timeout):
        raise HubSpotReadError("server down", status=503, retryable=True)

    client = HubSpotClient(settings, transport=transport, sleep=lambda _value: None)
    with pytest.raises(HubSpotReadError):
        client.pipelines()
    assert client.metrics.requests == settings.hubspot_retry_attempts


def test_multiple_exact_domain_candidates_manual_review(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    companies = [_company(), {**_company(), "id": "102"}]
    result = CRMScorer(configured, FakeScoringClient(companies=companies)).score(_account())
    assert result.tier == "manual_review"
    assert result.match_candidate_count == 2
    assert result.match_confidence == "ambiguous"


def test_primary_domain_then_same_label_alternate_domain_precedes_name(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    correct = {
        "id": "9001",
        "properties": {
            "name": "Example Bank Of Sample Town",
            "domain": "examplebank.com",
            "lifecyclestage": "",
        },
    }
    wrong_name = _company(
        name="Example Bank",
        domain="exampleholding.test",
        lifecyclestage="customer",
    )

    class DomainAwareClient(FakeScoringClient):
        def search_companies(self, property_name, value, limit=100):
            self.metrics.requests += 1
            if property_name == "domain" and value == "examplebank.com":
                return [correct]
            if property_name == "name":
                return [wrong_name, correct]
            return []

    account = replace(
        _account("examplebank.bank"),
        account_name="Example Bank",
        source_payload={"other_domains": ["examplebank.com", "exbank.test"]},
    )
    result = CRMScorer(configured, DomainAwareClient()).score(account)
    assert result.company_id == "9001"
    assert result.company_domain == "examplebank.com"
    assert result.match_basis == "exact_alternate_domain"
    assert result.tier == "recycle"


def test_production_policy_conservatively_suppresses_identity_ambiguity(settings, tmp_path):
    configured = replace(
        settings,
        hubspot_semantics_path=_semantics(
            tmp_path,
            identity_error_behavior="hard_skip",
            unresolved_evidence_behavior="hard_skip",
        ),
    )
    companies = [_company(), {**_company(), "id": "102"}]
    result = CRMScorer(configured, FakeScoringClient(companies=companies)).score(_account())
    assert result.tier == "hard_skip"
    assert result.reason == "conservative_unresolved_evidence_suppression"


def test_custom_open_stage_hard_skip_and_complete_counts(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    deals = [
        _deal("open", "open-stage", amount="10"),
        _deal("lost1", "lost-stage", closedate="2020-01-01T00:00:00Z"),
        _deal("lost2", "lost-stage", closedate="2021-01-01T00:00:00Z"),
    ]
    result = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()], deals=deals),
    ).score(_account())
    assert result.tier == "hard_skip"
    assert result.open_deal_count == 1
    assert result.closed_lost_count == 2
    assert result.latest_closed_lost_date == "2021-01-01"


def test_closed_won_without_customer_is_manual_review(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    result = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()], deals=[_deal("won", "won-stage")]),
    ).score(_account())
    assert result.tier == "manual_review"
    assert result.closed_won_count == 1
    assert "closed_won_without_customer_status" in result.evidence


def test_old_closed_lost_history_recycles(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    deals = [_deal(str(index), "lost-stage", closedate=f"202{index}-01-01T00:00:00Z") for index in range(3)]
    result = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()], deals=deals),
    ).score(_account())
    assert result.tier == "recycle"
    assert result.closed_lost_count == 3


def test_contact_email_dedupe_and_recent_contact_activity(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    contacts = [
        {
            "id": "c1",
            "properties": {
                "email": "Person@Example.com",
                "hs_additional_emails": "person@example.com;second@example.com",
                "notes_last_contacted": recent,
            },
        }
    ]
    result = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()], contacts=contacts),
    ).score(_account())
    assert result.associated_contact_count == 1
    assert result.business_email_count == 2
    assert result.latest_activity_source == "contact.notes_last_contacted"
    assert result.tier == "hard_skip"


def test_record_sources_are_exposed_and_creation_only_activity_is_ignored(settings, tmp_path):
    configured = replace(
        settings,
        hubspot_semantics_path=_semantics(
            tmp_path,
            meaningful_activity_properties={
                "companies": ["notes_last_updated"],
                "contacts": ["notes_last_updated"],
            },
        ),
    )
    created = "2025-11-24T10:53:00Z"
    contact = {
        "id": "c1",
        "properties": {
            "firstname": "Sam",
            "lastname": "Example",
            "email": "sam@example.com",
            "createdate": created,
            "notes_last_updated": created,
            "hs_object_source_label": "Offline Sources",
            "hs_object_source_detail_1": "campaign part 1.csv",
        },
    }
    company = _company(
        createdate="2017-04-07T13:54:00Z",
        notes_last_updated="2017-04-07T13:54:00Z",
        hs_object_source_label="CRM Setting",
    )
    result = CRMScorer(
        configured,
        FakeScoringClient(companies=[company], contacts=[contact]),
    ).score(_account())
    assert result.company_record_source == "CRM Setting"
    assert result.record_creation_only_activity_ignored is True
    assert result.latest_activity_date == ""
    assert result.tier == "recycle"
    assert "campaign part 1.csv" in result.contact_roster_summary


def test_request_cap_is_not_cached_as_manual_review(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))

    class CapClient(FakeScoringClient):
        def search_companies(self, property_name, value, limit=100):
            raise RequestCapExceeded("cap", category="request_cap")

    with pytest.raises(RequestCapExceeded):
        CRMScorer(configured, CapClient()).score(_account())


def test_no_contacts_is_distinct_from_contact_lookup_failure(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    no_contacts = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()]),
    ).score(_account())
    failed = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()], fail_contacts=True),
    ).score(_account())
    assert no_contacts.associated_contact_count == 0
    assert no_contacts.tier == "recycle"
    assert failed.tier == "manual_review"
    assert "contact_lookup_failed" in failed.lookup_error


def test_list_scope_failure_is_manual_review(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    result = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company()], fail_memberships=True),
    ).score(_account())
    assert result.tier == "manual_review"
    assert result.list_membership_status == "lookup_failed"


def test_verified_customer_and_inactive_status_values(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    customer = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company(hs_current_customer="yes")]),
    ).score(_account())
    inactive = CRMScorer(
        configured,
        FakeScoringClient(companies=[_company(account_status="inactive")]),
    ).score(_account())
    assert customer.tier == "hard_skip"
    assert customer.reason == "current_customer_lifecycle"
    assert inactive.tier == "manual_review"
    assert "inactive_or_stub_status" in inactive.evidence


def test_missing_domain_no_match_never_becomes_net_new(settings, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    result = CRMScorer(configured, FakeScoringClient()).score(_account(domain=""))
    assert result.tier == "manual_review"


def test_cache_hits_idempotency_and_plan(settings, conn, tmp_path):
    configured = replace(
        settings,
        hubspot_semantics_path=_semantics(tmp_path),
        hubspot_checkpoint_batch_size=1,
    )
    accounts = [_account("new.example")]
    first_client = FakeScoringClient()
    first = run_crm_batch(
        conn,
        configured,
        accounts,
        output_dir=tmp_path / "first",
        client=first_client,
    )
    second_client = FakeScoringClient()
    second = run_crm_batch(
        conn,
        configured,
        accounts,
        output_dir=tmp_path / "second",
        client=second_client,
    )
    assert first["dispositions"]["net_new"] == 1
    assert second["cache_hits"] == 1
    assert second_client.metrics.requests == 0
    assert len(db.fetch_crm_scores(conn, batch_source="fixture", cohort="test")) == 1
    assert plan_crm_batch(conn, configured, accounts)["cache_hits"] == 1
    assert db.get_crm_cache(conn, "domain:new.example")["fetched_at"]


def test_checkpoint_resume_after_interruption(settings, conn, tmp_path):
    configured = replace(
        settings,
        hubspot_semantics_path=_semantics(tmp_path),
        hubspot_checkpoint_batch_size=1,
    )
    accounts = [_account("one.example"), _account("two.example")]
    with pytest.raises(InterruptedError):
        run_crm_batch(
            conn,
            configured,
            accounts,
            output_dir=tmp_path / "interrupted",
            client=FakeScoringClient(),
            interrupt_after=1,
        )
    resumed_client = FakeScoringClient()
    resumed = run_crm_batch(
        conn,
        configured,
        accounts,
        output_dir=tmp_path / "resumed",
        client=resumed_client,
        resume=True,
    )
    assert resumed["resumed_from_index"] == 1
    assert resumed["processed"] == 2
    assert resumed["cache_hits"] == 1
    assert resumed["isolation"]["hubspot_only"] is True
    assert resumed["isolation"]["hubspot_writeback"] is False


def test_crm_csv_and_jsonl_export_have_same_structured_fields(settings, conn, tmp_path):
    configured = replace(settings, hubspot_semantics_path=_semantics(tmp_path))
    output = tmp_path / "exports"
    run_crm_batch(
        conn,
        configured,
        [_account("parity.example")],
        output_dir=output,
        client=FakeScoringClient(),
    )
    csv_header = (output / "crm_account_scores.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    json_row = json.loads((output / "crm_account_scores.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert set(csv_header) == set(json_row)
    assert {"tier", "reason", "associated_deal_count", "associated_contact_count"} <= set(json_row)
    assert set(CRMScoreResult.__dataclass_fields__) | set(SOURCE_CONTEXT_COLUMNS) <= set(json_row)


def test_example_semantics_fail_closed():
    semantics = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "config"
            / "hubspot_portal_semantics.example.json"
        ).read_text(encoding="utf-8")
    )
    behavior_keys = {
        "closed_won_without_customer_behavior",
        "configured_list_member_behavior",
        "identity_error_behavior",
        "list_membership_error_behavior",
        "lookup_error_behavior",
        "unknown_stage_behavior",
        "unresolved_evidence_behavior",
    }
    assert {key: semantics.get(key) for key in behavior_keys} == {
        key: "hard_skip" for key in behavior_keys
    }

