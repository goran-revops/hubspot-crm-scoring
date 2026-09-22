from __future__ import annotations

import json
import re
from pathlib import Path


def normalize_company_name(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    stopwords = {
        "the",
        "of",
        "city",
        "county",
        "borough",
        "town",
        "township",
        "state",
        "department",
        "dept",
        "public",
        "schools",
        "school",
        "district",
        "no",
    }
    parts = [part for part in text.split() if part and part not in stopwords]
    return " ".join(parts)


def load_list_ids(path: Path | None = None) -> set[str]:
    if path is None or not Path(path).exists():
        return set()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    lists = data.get("lists") if isinstance(data, dict) else []
    ids: set[str] = set()
    for item in lists or []:
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
        elif isinstance(item, (str, int)):
            ids.add(str(item))
    return ids
