from __future__ import annotations

from datetime import datetime, timezone

from .models import JobRecord, RawJob
from .normalization import (
    clean_text,
    content_hash,
    extract_salary,
    infer_category,
    infer_company,
    make_summary,
    normalize_employment_type,
    normalize_location,
    normalize_remote_status,
    normalize_url,
)
from .parsers import SourceConfig


def map_raw_job(raw: RawJob, config: SourceConfig) -> JobRecord:
    title = clean_text(raw.title)
    body = clean_text(raw.summary or raw.description_html or "", max_spaces=False)
    company = clean_text(raw.company) or infer_company(title, body)
    location = normalize_location(raw.location, title=title, text=body, default=config.default_location)
    category = clean_text(raw.category) or infer_category(title, body, default=config.default_category)
    employment_type = clean_text(raw.employment_type) or normalize_employment_type(body)
    salary_range = clean_text(raw.salary_range) or extract_salary(body)
    remote_status = clean_text(raw.remote_status) or normalize_remote_status(location, body)
    apply_url = normalize_url(raw.apply_url, raw.source_url) or normalize_url(raw.source_url) or ""
    summary = make_summary(title, body)

    # Preserve traceability even when the existing DB has only the legacy `summary` column.
    source_line = f"\n\nSource: {raw.source_name} | {raw.source_url}"
    if raw.expires_at:
        source_line += f" | Deadline: {raw.expires_at}"
    if employment_type:
        source_line += f" | Type: {employment_type}"
    if salary_range:
        source_line += f" | Salary: {salary_range}"
    if len(summary) + len(source_line) <= 1200:
        summary = f"{summary}{source_line}"

    digest = content_hash([title, company, location, summary, apply_url, raw.source_url])
    return JobRecord(
        title=title,
        company=company,
        location=location,
        category=category,
        summary=summary,
        apply_url=apply_url,
        featured=0,
        source_name=raw.source_name,
        source_url=raw.source_url,
        posted_at=raw.posted_at,
        expires_at=raw.expires_at,
        employment_type=employment_type,
        salary_range=salary_range,
        remote_status=remote_status,
        content_hash=digest,
        scraped_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
