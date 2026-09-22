# HubSpot CRM scoring

This scores a company against your HubSpot and returns one answer: skip them, worth another look, or not in HubSpot.

A script can call it. An app can call it. A job can call it. One use is a lead list before outreach: you have many companies, and you want to know which ones are safe to contact now. The check is the same no matter what sends the row.

For each company it searches HubSpot by domain. If that misses, it tries other domains on the same row. A hit on the company name alone is written down and ignored. It is not a match.

When the domain matches one company, it loads every deal on that company and every contact on that company. It also checks lists, if you named any. Those records are rolled into one answer:

| Answer | Meaning |
| --- | --- |
| `hard_skip` | Skip them. They are a customer, a deal is open, someone was in touch recently, or the lookup was not safe enough to guess. |
| `recycle` | Worth another look. They are in HubSpot, and nothing recent says to stay away. An old Closed Lost deal lands here. |
| `net_new` | Not in HubSpot. The domain search finished with no company. |
| `manual_review` | A person should check. The example settings do not use this. They skip the row instead of guessing. |

A long file is normal. One run stops after 500 HubSpot requests, then `--resume` continues the same file. `--plan` calls HubSpot zero times.

It does not read every field in the portal. It reads companies, the deals on those companies, the contacts on those companies, and lists you name. It does not create or update HubSpot records.

## Company fields HubSpot already has

These names are HubSpot's. A normal portal has them. You do not create them.

| Field | What it is used for |
| --- | --- |
| `name`, `domain` | Find the company. |
| `lifecyclestage` | In the example settings, `customer` or `evangelist` means current customer. Change `customer_lifecycle_values` if your portal uses other values. |
| `hubspot_owner_id` | Copied onto the result. It does not pick the tier. |
| `createdate` | When the company record was created. |
| `notes_last_contacted` | Last time someone was contacted. |
| `notes_last_updated` | Ignored when it is within 60 seconds of `createdate`. That is the record being created, not a sales touch. |
| `hs_last_sales_activity_timestamp` | Last sales activity. |
| `hs_last_booked_meeting_date`, `engagements_last_meeting_booked`, `hs_latest_meeting_activity` | Meetings. |
| `hs_last_logged_call_date` | Logged calls. |
| `hs_last_logged_outgoing_email_date`, `hs_sales_email_last_replied` | Logged email and replies. |
| `hs_last_sales_activity_type` | What the last sales activity was. Copied onto the result. |
| `hs_object_source`, `hs_object_source_label`, `hs_object_source_detail_1`, `hs_object_source_detail_2`, `hs_object_source_detail_3` | How the record got into HubSpot. Copied onto the result. |
| `hs_analytics_source`, `hs_analytics_source_data_1`, `hs_analytics_source_data_2` | Original analytics source. Copied onto the result. |

`hs_lastmodifieddate` is not read. Editing a field is not a sales touch.

## Your own status field

The example file does not name a status property. If you have one, set `account_status_property` to that property's internal name, and put the values that mean inactive in `inactive_status_values`.

`customer_status_fields` is where a custom "this is a customer" property goes. `discover` fills `hs_current_customer` only when your portal already has that HubSpot property. The company search asks HubSpot for extra names only when they are still in this file.

## Deal fields

It loads every deal associated with the matched company, and reads:

`dealname`, `dealstage`, `pipeline`, `amount`, `closedate`, `createdate`, `hs_lastmodifieddate`, `hs_is_closed`, `hs_is_closed_won`, `hs_is_closed_lost`, `hs_closed_lost_reason`.

An open deal means leave them alone. Closed won can mean the same, depending on settings. An old closed lost deal, with nothing else recent, means recycle.

`discover` fills `deal_stages` from your portal so a stage id can be read as open, closed won, or closed lost. An unknown stage is not treated as safe.

## Contact fields

It loads every contact associated with the matched company, and reads:

`email`, `work_email`, `hs_additional_emails`, `firstname`, `lastname`, `jobtitle`, `lifecyclestage`, `createdate`, plus the same activity fields and source fields as the company.

Contact email is evidence that the person exists. It does not, by itself, make the company a customer. The same activity dates on a contact can still mean someone talked to them recently.

## Lists

If you put list IDs in `config/hubspot_lists.json`, a company on one of those lists is `hard_skip`. No file, or an empty list, means list membership is not checked.

## What the output row contains

`tier` and `reason` are the decision. The row also keeps the HubSpot company id, the match type (domain, alternate domain, or name-only), the lifecycle stage, the owner id, the latest activity date and which field it came from, deal counts, and a short roster of up to 10 contacts.

## Setup

Python 3.11+.

```powershell
python -m pip install -e .
copy .env.example .env
```

Put a HubSpot private-app token in `.env` as `HUBSPOT_ACCESS_TOKEN`.

Read scopes:

- `crm.objects.companies.read`
- `crm.objects.contacts.read`
- `crm.objects.deals.read`
- `crm.schemas.companies.read`
- `crm.schemas.contacts.read`
- `crm.schemas.deals.read`
- `crm.lists.read` (only if you configure list IDs)

The first run copies `config/hubspot_portal_semantics.example.json` to `config/hubspot_portal_semantics.json` if that file is missing.

## Discover your portal

```powershell
python -m hubspot_crm discover
```

This fills `deal_stages` and customer-status options from your portal. It does not write to HubSpot.

## Score a file

Input is CSV, JSON, or JSONL. Required column: `domain` or `company_domain`. Name column: `account_name`, `company_name`, or `organization_name`.

```powershell
python -m hubspot_crm score --input examples/accounts.example.jsonl --plan
python -m hubspot_crm score --input examples/accounts.example.jsonl --output-dir out/crm
python -m hubspot_crm score --input examples/accounts.example.jsonl --output-dir out/crm --resume
```

Outputs:

- `out/crm/crm_account_scores.csv`
- `out/crm/crm_account_scores.jsonl`
- `out/crm/crm_score_summary.json`

## Tests

```powershell
python -m pip install -e ".[dev]"
$env:PYTHONPATH = "src"
python -m pytest
```

Tests do not call HubSpot.
