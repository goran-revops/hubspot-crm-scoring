from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    hubspot_access_token: str
    hubspot_recent_activity_days: int
    hubspot_request_cap: int = 500
    hubspot_retry_attempts: int = 3
    hubspot_retry_backoff_seconds: int = 1
    hubspot_association_page_limit: int = 100
    hubspot_association_page_guard: int = 100
    hubspot_batch_read_size: int = 100
    hubspot_cache_ttl_seconds: int = 86400
    hubspot_checkpoint_batch_size: int = 25
    hubspot_semantics_path: Path = Path("config/hubspot_portal_semantics.json")
    hubspot_lists_path: Path = Path("config/hubspot_lists.json")

    @property
    def sqlite_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("DATABASE_URL must start with sqlite:///")
        return Path(self.database_url[len(prefix) :])


def load_settings(env_path: str | Path = ".env") -> Settings:
    load_dotenv(env_path)
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/crm_scores.db"),
        hubspot_access_token=os.getenv("HUBSPOT_ACCESS_TOKEN", ""),
        hubspot_recent_activity_days=env_int("HUBSPOT_RECENT_ACTIVITY_DAYS", 730),
        hubspot_request_cap=env_int("HUBSPOT_REQUEST_CAP", 500),
        hubspot_retry_attempts=env_int("HUBSPOT_RETRY_ATTEMPTS", 3),
        hubspot_retry_backoff_seconds=env_int("HUBSPOT_RETRY_BACKOFF_SECONDS", 1),
        hubspot_association_page_limit=env_int("HUBSPOT_ASSOCIATION_PAGE_LIMIT", 100),
        hubspot_association_page_guard=env_int("HUBSPOT_ASSOCIATION_PAGE_GUARD", 100),
        hubspot_batch_read_size=env_int("HUBSPOT_BATCH_READ_SIZE", 100),
        hubspot_cache_ttl_seconds=env_int("HUBSPOT_CACHE_TTL_SECONDS", 86400),
        hubspot_checkpoint_batch_size=env_int("HUBSPOT_CHECKPOINT_BATCH_SIZE", 25),
        hubspot_semantics_path=Path(
            os.getenv("HUBSPOT_SEMANTICS_PATH", "config/hubspot_portal_semantics.json")
        ),
        hubspot_lists_path=Path(os.getenv("HUBSPOT_LISTS_PATH", "config/hubspot_lists.json")),
    )
