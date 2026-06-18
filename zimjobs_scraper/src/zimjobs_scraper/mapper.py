from __future__ import annotations

from datetime import datetime, timezone

from .models import JobRecord, RawJob
from .normalization import (
    clean_job_title,
    clean_text,
    content_hash,
    extract_labeled_value,
    extract_salary,
    extract_section,
    find_deadline,
    infer_company,
    looks_like_good_company,
    make_summary,
    normalize_job_text,
    normalize_category,
    normalize_employment_type,
    normalize_location,
    normalize_remote_status,
    normalize_url,
    resolve_vacancy_mail_zimbabwe_company,
)
from .parsers import SourceConfig


def map_raw_job(raw: RawJob, config: SourceConfig) -> JobRecord:
    original_title = clean_text(raw.title)
    body = normalize_job_text(raw.summary) or clean_html_body(raw.description_html)
    body = normalize_job_text(body, max_chars=12000)

    parsed_company = clean_text(raw.company)
    if looks_like_good_company(parsed_company):
        company = parsed_company
    else:
        inferred_company = infer_company(original_title, body)
        company = inferred_company if looks_like_good_company(inferred_company) else clean_text(config.default_company) or "Confidential"
    location = normalize_location(raw.location, title=original_title, text=body, default=config.default_location)
    corrected_company = resolve_vacancy_mail_zimbabwe_company(
        company,
        original_title,
        body,
        location,
        source_name=raw.source_name or config.name,
        source_url=raw.source_url,
        apply_url=raw.apply_url,
    )
    if corrected_company:
        company = corrected_company
    title = clean_job_title(original_title, company=company, text=body)
    category = normalize_category(raw.category or config.default_category, title=f"{original_title} {title}", location=location, text=body, default=config.default_category)
    employment_type = clean_text(raw.employment_type) or normalize_employment_type(body)
    salary_range = clean_text(raw.salary_range) or extract_salary(body)
    expires_at = raw.expires_at or find_deadline(f"{original_title}\n{body}")
    remote_status = clean_text(raw.remote_status) or normalize_remote_status(location, body)
    department = clean_text(raw.department) or extract_labeled_value(body, ["Department", "Team", "Unit", "Programme", "Program"])
    requirements = normalize_job_text(raw.requirements, max_chars=1800) or extract_section(
        body,
        [
            "Requirements",
            "Qualifications",
            "Qualifications and Experience",
            "Required Skills",
            "Skills and Experience",
            "Education and Experience",
            "Candidate Profile",
        ],
    )
    source_url = normalize_url(raw.source_url) or ""
    apply_url = normalize_url(raw.apply_url, source_url) or source_url
    summary = normalize_job_text(make_summary(original_title, body), max_chars=900)

    # Preserve traceability even when the existing DB has only the legacy `summary` column.
    meta_bits = [f"Source: {raw.source_name}"]
    if source_url:
        meta_bits.append(source_url)
    if expires_at:
        meta_bits.append(f"Deadline: {expires_at}")
    if employment_type:
        meta_bits.append(f"Type: {employment_type}")
    if department:
        meta_bits.append(f"Department: {department}")
    if salary_range:
        meta_bits.append(f"Salary: {salary_range}")
    if remote_status and remote_status != "On-site":
        meta_bits.append(f"Workplace: {remote_status}")
    source_line = "\n\n" + " | ".join(meta_bits)
    if len(summary) + len(source_line) <= 1100:
        summary = normalize_job_text(f"{summary}{source_line}", max_chars=1100, remove_noise=False)

    digest = content_hash([title, company, location, summary, apply_url, source_url, raw.external_id])
    return JobRecord(
        title=title,
        company=company,
        location=location,
        category=category,
        summary=summary,
        apply_url=apply_url,
        featured=0,
        source_name=raw.source_name,
        source_url=source_url,
        posted_at=raw.posted_at,
        expires_at=expires_at,
        department=department,
        employment_type=employment_type,
        salary_range=salary_range,
        remote_status=remote_status,
        job_description=body,
        requirements=requirements,
        external_job_id=clean_text(raw.external_id) or None,
        content_hash=digest,
        scraped_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )


def clean_html_body(value: str | None) -> str:
    from .normalization import clean_html_to_markdownish

    return normalize_job_text(clean_html_to_markdownish(value))
