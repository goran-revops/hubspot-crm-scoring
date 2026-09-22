from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from hubspot_crm import db
from hubspot_crm.config import load_settings
from hubspot_crm.crm_scoring import load_canonical_accounts, plan_crm_batch, run_crm_batch
from hubspot_crm.hubspot_portal import discover_portal

EXAMPLE_SEMANTICS = Path("config/hubspot_portal_semantics.example.json")


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=True))


def _ensure_semantics(path: Path) -> None:
    if path.exists():
        return
    if not EXAMPLE_SEMANTICS.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy {EXAMPLE_SEMANTICS} to that path before scoring."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_SEMANTICS, path)


def _prepare(args: argparse.Namespace):
    settings = load_settings(args.env_file)
    _ensure_semantics(settings.hubspot_semantics_path)
    return settings


def cmd_discover(args: argparse.Namespace) -> int:
    settings = _prepare(args)
    result = discover_portal(settings)
    _print_json(
        {
            "status": "ok",
            "mode": "read_only",
            "semantics_path": str(settings.hubspot_semantics_path),
            **result,
        }
    )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    settings = _prepare(args)
    accounts = load_canonical_accounts(Path(args.input))
    selected = accounts[: args.limit] if args.limit is not None else accounts
    with db.db_connection(settings) as conn:
        db.init_db(conn)
        if args.plan:
            result = plan_crm_batch(conn, settings, selected)
        else:
            result = run_crm_batch(
                conn,
                settings,
                selected,
                output_dir=Path(args.output_dir),
                resume=args.resume,
            )
    _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only HubSpot CRM scoring")
    parser.add_argument("--env-file", default=".env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Read portal stages and write semantics")
    discover.set_defaults(func=cmd_discover)

    score = subparsers.add_parser("score", help="Score a CSV, JSON, or JSONL account file")
    score.add_argument("--input", required=True)
    score.add_argument("--plan", action="store_true")
    score.add_argument("--limit", type=int)
    score.add_argument("--resume", action="store_true")
    score.add_argument("--output-dir", default="out/crm_score")
    score.set_defaults(func=cmd_score)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)
