from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class RawJob:
    """A job as found on a source before normalization."""

    source_name: str
    source_url: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    category: str | None = None
    summary: str | None = None
    description_html: str | None = None
    apply_url: str | None = None
    posted_at: str | None = None
    expires_at: str | None = None
    department: str | None = None
    employment_type: str | None = None
    salary_range: str | None = None
    remote_status: str | None = None
    external_id: str | None = None
    requirements: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JobRecord:
    """Normalized job record ready for validation, dedupe and DB writing."""

    title: str
    company: str
    location: str
    category: str
    summary: str
    apply_url: str
    featured: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    # Optional metadata. Inserted only when corresponding columns exist or AUTO_ADD_OPTIONAL_COLUMNS=1.
    source_name: str | None = None
    source_url: str | None = None
    posted_at: str | None = None
    expires_at: str | None = None
    department: str | None = None
    employment_type: str | None = None
    salary_range: str | None = None
    remote_status: str | None = None
    job_description: str | None = None
    requirements: str | None = None
    external_job_id: str | None = None
    content_hash: str | None = None
    scraped_at: str | None = None
