from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .models import JobRecord
from .normalization import is_expired


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


class JobValidator:
    def __init__(self, skip_expired: bool = True, allowed_locations: list[str] | None = None):
        self.skip_expired = skip_expired
        self.allowed_locations = allowed_locations or []

    def validate(self, job: JobRecord) -> ValidationResult:
        reasons: list[str] = []
        if len(job.title) < 5:
            reasons.append("title_too_short")
        if len(job.company) < 2:
            reasons.append("company_missing")
        if len(job.summary) < 80:
            reasons.append("summary_too_short")
        parsed = urlparse(job.apply_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            reasons.append("apply_url_invalid")
        if self.skip_expired and is_expired(job.expires_at):
            reasons.append("expired")
        if self.allowed_locations:
            haystack = f"{job.location} {job.title} {job.summary}".lower()
            if not any(loc.lower() in haystack for loc in self.allowed_locations):
                reasons.append("outside_allowed_locations")
        if re.search(r"casino|betting|adult|crypto giveaway|get rich quick", job.title + " " + job.summary, re.I):
            reasons.append("low_quality_or_spam")
        return ValidationResult(ok=not reasons, reasons=reasons)
