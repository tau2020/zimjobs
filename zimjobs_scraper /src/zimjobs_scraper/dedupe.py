from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable
from urllib.parse import urlparse

from .models import JobRecord
from .normalization import clean_text


def normalized_key(job: JobRecord) -> str:
    parts = [job.title, job.company, job.location]
    normalized = "|".join(re.sub(r"\W+", " ", clean_text(p).lower()).strip() for p in parts)
    return normalized


def canonical_url_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    return f"{parsed.netloc.lower()}{path}"


def similar(a: str, b: str) -> float:
    a_norm = re.sub(r"\W+", " ", clean_text(a).lower())[:2000]
    b_norm = re.sub(r"\W+", " ", clean_text(b).lower())[:2000]
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def dedupe_in_memory(jobs: Iterable[JobRecord], similarity_threshold: float = 0.88) -> list[JobRecord]:
    accepted: list[JobRecord] = []
    seen_urls: set[str] = set()
    seen_keys: dict[str, JobRecord] = {}
    for job in jobs:
        url_key = canonical_url_key(job.apply_url)
        if url_key in seen_urls:
            continue
        key = normalized_key(job)
        existing = seen_keys.get(key)
        if existing and similar(existing.summary, job.summary) >= similarity_threshold:
            continue
        seen_urls.add(url_key)
        seen_keys[key] = job
        accepted.append(job)
    return accepted
