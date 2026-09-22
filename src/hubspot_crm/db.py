from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from hubspot_crm.config import Settings


def connect(settings: Settings) -> sqlite3.Connection:
    path = settings.sqlite_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_connection(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crm_account_scores (
          canonical_key TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          cohort TEXT NOT NULL DEFAULT '',
          source_record_id TEXT NOT NULL DEFAULT '',
          account_name TEXT NOT NULL DEFAULT '',
          domain TEXT NOT NULL DEFAULT '',
          provenance TEXT NOT NULL DEFAULT '',
          company_found INTEGER NOT NULL DEFAULT 0,
          company_id TEXT NOT NULL DEFAULT '',
          company_name TEXT NOT NULL DEFAULT '',
          company_domain TEXT NOT NULL DEFAULT '',
          company_created_at TEXT NOT NULL DEFAULT '',
          company_record_source TEXT NOT NULL DEFAULT '',
          company_record_source_details TEXT NOT NULL DEFAULT '',
          lifecycle_stage TEXT NOT NULL DEFAULT '',
          account_status TEXT NOT NULL DEFAULT '',
          owner_id TEXT NOT NULL DEFAULT '',
          match_basis TEXT NOT NULL DEFAULT '',
          match_confidence TEXT NOT NULL DEFAULT '',
          match_candidate_count INTEGER NOT NULL DEFAULT 0,
          match_candidates_summary TEXT NOT NULL DEFAULT '',
          list_membership_status TEXT NOT NULL DEFAULT 'not_checked',
          list_membership_ids TEXT NOT NULL DEFAULT '',
          list_membership_error TEXT NOT NULL DEFAULT '',
          latest_activity_date TEXT NOT NULL DEFAULT '',
          latest_activity_type TEXT NOT NULL DEFAULT '',
          latest_activity_source TEXT NOT NULL DEFAULT '',
          associated_contact_count INTEGER NOT NULL DEFAULT 0,
          business_email_count INTEGER NOT NULL DEFAULT 0,
          business_email_summary TEXT NOT NULL DEFAULT '',
          contact_roster_summary TEXT NOT NULL DEFAULT '',
          contact_evidence_coverage TEXT NOT NULL DEFAULT '',
          record_creation_only_activity_ignored INTEGER NOT NULL DEFAULT 0,
          contact_usage_policy TEXT NOT NULL DEFAULT 'hubspot_evidence_only',
          associated_deal_count INTEGER NOT NULL DEFAULT 0,
          open_deal_count INTEGER NOT NULL DEFAULT 0,
          latest_open_deal_id TEXT NOT NULL DEFAULT '',
          latest_open_deal_stage TEXT NOT NULL DEFAULT '',
          latest_open_deal_date TEXT NOT NULL DEFAULT '',
          latest_open_deal_amount TEXT NOT NULL DEFAULT '',
          closed_won_count INTEGER NOT NULL DEFAULT 0,
          latest_closed_won_date TEXT NOT NULL DEFAULT '',
          closed_lost_count INTEGER NOT NULL DEFAULT 0,
          latest_closed_lost_date TEXT NOT NULL DEFAULT '',
          latest_closed_lost_stage TEXT NOT NULL DEFAULT '',
          latest_closed_lost_amount TEXT NOT NULL DEFAULT '',
          deal_conflict TEXT NOT NULL DEFAULT '',
          lookup_error TEXT NOT NULL DEFAULT '',
          scope_property_errors TEXT NOT NULL DEFAULT '',
          tier TEXT NOT NULL DEFAULT 'manual_review',
          reason TEXT NOT NULL DEFAULT '',
          evidence TEXT NOT NULL DEFAULT '',
          cache_status TEXT NOT NULL DEFAULT 'miss',
          cache_fetched_at TEXT NOT NULL DEFAULT '',
          scored_at TEXT NOT NULL DEFAULT '',
          source_payload_json TEXT NOT NULL DEFAULT '{}',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (canonical_key, source, cohort, source_record_id)
        );

        CREATE INDEX IF NOT EXISTS idx_crm_scores_company_id
          ON crm_account_scores(company_id);
        CREATE INDEX IF NOT EXISTS idx_crm_scores_tier
          ON crm_account_scores(tier);

        CREATE TABLE IF NOT EXISTS crm_score_cache (
          cache_key TEXT PRIMARY KEY,
          company_id TEXT NOT NULL DEFAULT '',
          result_json TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_crm_cache_company_id
          ON crm_score_cache(company_id);

        CREATE TABLE IF NOT EXISTS crm_batch_runs (
          id TEXT PRIMARY KEY,
          input_fingerprint TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          cohort TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          total_accounts INTEGER NOT NULL DEFAULT 0,
          processed_accounts INTEGER NOT NULL DEFAULT 0,
          checkpoint_index INTEGER NOT NULL DEFAULT 0,
          request_count INTEGER NOT NULL DEFAULT 0,
          cache_hits INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          output_dir TEXT NOT NULL DEFAULT '',
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          error_message TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS crm_batch_locks (
          input_fingerprint TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        """
    )


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_crm_cache(conn: sqlite3.Connection, cache_key: str, *, now: str | None = None) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM crm_score_cache
        WHERE cache_key = ? AND expires_at > ?
        """,
        (cache_key, now or now_iso()),
    ).fetchone()
    return row_to_dict(row)


def upsert_crm_cache(
    conn: sqlite3.Connection,
    *,
    cache_key: str,
    company_id: str,
    result_json: str,
    fetched_at: str,
    expires_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO crm_score_cache(cache_key, company_id, result_json, fetched_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
          company_id=excluded.company_id,
          result_json=excluded.result_json,
          fetched_at=excluded.fetched_at,
          expires_at=excluded.expires_at
        """,
        (cache_key, company_id, result_json, fetched_at, expires_at),
    )


def upsert_crm_score(
    conn: sqlite3.Connection,
    result: dict[str, Any],
    *,
    source_payload: dict[str, Any] | None = None,
) -> None:
    fields = [
        "canonical_key", "source", "cohort", "source_record_id", "account_name", "domain",
        "provenance", "company_found", "company_id", "company_name", "company_domain",
        "company_created_at", "company_record_source", "company_record_source_details",
        "lifecycle_stage",
        "account_status", "owner_id", "match_basis", "match_confidence",
        "match_candidate_count", "match_candidates_summary", "list_membership_status",
        "list_membership_ids", "list_membership_error", "latest_activity_date",
        "latest_activity_type", "latest_activity_source", "associated_contact_count",
        "business_email_count", "business_email_summary", "contact_roster_summary",
        "contact_evidence_coverage", "record_creation_only_activity_ignored",
        "contact_usage_policy", "associated_deal_count",
        "open_deal_count", "latest_open_deal_id", "latest_open_deal_stage",
        "latest_open_deal_date", "latest_open_deal_amount", "closed_won_count",
        "latest_closed_won_date", "closed_lost_count", "latest_closed_lost_date",
        "latest_closed_lost_stage", "latest_closed_lost_amount", "deal_conflict",
        "lookup_error", "scope_property_errors", "tier", "reason", "evidence",
        "cache_status", "cache_fetched_at", "scored_at",
    ]
    values = []
    for field_name in fields:
        value = result.get(field_name, "")
        if field_name in {"company_found", "record_creation_only_activity_ignored"}:
            value = int(bool(value))
        values.append(value)
    fields_with_payload = fields + ["source_payload_json"]
    values.append(json.dumps(source_payload or {}, sort_keys=True, ensure_ascii=True))
    assignments = ", ".join(
        f"{field_name}=excluded.{field_name}"
        for field_name in fields_with_payload
        if field_name not in {"canonical_key", "source", "cohort", "source_record_id"}
    )
    conn.execute(
        f"""
        INSERT INTO crm_account_scores ({", ".join(fields_with_payload)})
        VALUES ({", ".join("?" for _ in fields_with_payload)})
        ON CONFLICT(canonical_key, source, cohort, source_record_id)
        DO UPDATE SET {assignments}, updated_at=CURRENT_TIMESTAMP
        """,
        values,
    )


def fetch_crm_scores(conn: sqlite3.Connection, *, batch_source: str = "", cohort: str = "") -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if batch_source:
        clauses.append("source = ?")
        values.append(batch_source)
    if cohort:
        clauses.append("cohort = ?")
        values.append(cohort)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM crm_account_scores" + where + " ORDER BY canonical_key, source_record_id",
        values,
    ).fetchall()
    return [dict(row) for row in rows]


def acquire_crm_batch_lock(conn: sqlite3.Connection, input_fingerprint: str, batch_id: str) -> None:
    try:
        conn.execute(
            "INSERT INTO crm_batch_locks(input_fingerprint, batch_id, acquired_at) VALUES (?, ?, ?)",
            (input_fingerprint, batch_id, now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            "SELECT batch_id FROM crm_batch_locks WHERE input_fingerprint = ?",
            (input_fingerprint,),
        ).fetchone()
        owner = str(row["batch_id"]) if row else "unknown"
        raise RuntimeError(f"CRM batch fingerprint is already locked by batch {owner}") from exc


def release_crm_batch_lock(conn: sqlite3.Connection, input_fingerprint: str, batch_id: str) -> None:
    conn.execute(
        "DELETE FROM crm_batch_locks WHERE input_fingerprint = ? AND batch_id = ?",
        (input_fingerprint, batch_id),
    )


def create_crm_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    input_fingerprint: str,
    source: str,
    cohort: str,
    total_accounts: int,
    output_dir: str,
) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO crm_batch_runs(
          id, input_fingerprint, source, cohort, status, total_accounts,
          output_dir, started_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (batch_id, input_fingerprint, source, cohort, total_accounts, output_dir, timestamp, timestamp),
    )


def update_crm_batch(conn: sqlite3.Connection, batch_id: str, **fields: Any) -> None:
    allowed = {
        "status", "processed_accounts", "checkpoint_index", "request_count",
        "cache_hits", "error_count", "error_message",
    }
    selected = {key: value for key, value in fields.items() if key in allowed}
    if not selected:
        return
    selected["updated_at"] = now_iso()
    conn.execute(
        f"UPDATE crm_batch_runs SET {', '.join(f'{key} = ?' for key in selected)} WHERE id = ?",
        [*selected.values(), batch_id],
    )


def latest_resumable_crm_batch(
    conn: sqlite3.Connection,
    input_fingerprint: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM crm_batch_runs
        WHERE input_fingerprint = ? AND status IN ('running', 'failed', 'interrupted')
        ORDER BY started_at DESC LIMIT 1
        """,
        (input_fingerprint,),
    ).fetchone()
    return row_to_dict(row)
