from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse

from .models import JobRecord
from .normalization import clean_text


def normalized_key(job: JobRecord) -> str:
    parts = [job.title, job.company, job.location]
    normalized = "|".join(re.sub(r"\W+", " ", clean_text(p).lower()).strip() for p in parts)
    return normalized


def canonical_url_key(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/").lower()
    ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
    query_parts = [
        (key.lower(), value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in ignored
    ]
    query = urlencode(sorted(query_parts))
    return f"{parsed.netloc.lower()}{path}" + (f"?{query}" if query else "")


def identity_keys(job: JobRecord) -> list[str]:
    if job.source_name and job.external_job_id:
        external = re.sub(r"\W+", " ", clean_text(job.external_job_id).lower()).strip()
        if external:
            return [f"external:{clean_text(job.source_name).lower()}:{external}"]
    keys = []
    source_url = canonical_url_key(job.source_url or "")
    apply_url = canonical_url_key(job.apply_url)
    if source_url:
        keys.append(f"source:{source_url}")
    if apply_url and apply_url != source_url:
        keys.append(f"apply:{apply_url}")
    return keys


def similar(a: str, b: str) -> float:
    a_norm = re.sub(r"\W+", " ", clean_text(a).lower())[:2000]
    b_norm = re.sub(r"\W+", " ", clean_text(b).lower())[:2000]
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def dedupe_in_memory(jobs: Iterable[JobRecord], similarity_threshold: float = 0.88) -> list[JobRecord]:
    accepted: list[JobRecord] = []
    seen_identities: dict[str, str] = {}
    seen_keys: dict[str, JobRecord] = {}
    for job in jobs:
        key = normalized_key(job)
        identities = identity_keys(job)
        if any(identity.startswith("external:") and identity in seen_identities for identity in identities):
            continue
        if any(seen_identities.get(identity) == key for identity in identities):
            continue
        existing = seen_keys.get(key)
        if existing and similar(existing.summary, job.summary) >= similarity_threshold:
            continue
        for identity in identities:
            seen_identities[identity] = key
        seen_keys[key] = job
        accepted.append(job)
    return accepted
