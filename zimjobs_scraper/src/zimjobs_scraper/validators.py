from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .models import JobRecord
from .normalization import is_expired, looks_like_good_company, looks_like_real_role


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


REMOTE_RESTRICTED_ONLY_RE = re.compile(
    r"\b(?:"
    r"u\.?s\.?\s+only|usa\s+only|united\s+states\s+only|must\s+be\s+(?:based|located|resident)\s+in\s+(?:the\s+)?(?:u\.?s\.?|usa|united\s+states)|"
    r"uk\s+only|united\s+kingdom\s+only|canada\s+only|europe\s+only|eu\s+only|australia\s+only|new\s+zealand\s+only|"
    r"only\s+(?:candidates|applicants)\s+(?:based|located|resident)\s+in\s+(?:the\s+)?(?:u\.?s\.?|usa|united\s+states|uk|united\s+kingdom|canada|europe|eu|australia|new\s+zealand)"
    r")\b",
    re.I,
)

REMOTE_ALLOWED_RE = re.compile(
    r"\b(?:worldwide|anywhere\s+in\s+the\s+world|global|international|emea|africa|zimbabwe|sast|south\s+african\s+standard\s+time|utc\+?2)\b",
    re.I,
)


class JobValidator:
    def __init__(self, skip_expired: bool = True, allowed_locations: list[str] | None = None):
        self.skip_expired = skip_expired
        self.allowed_locations = allowed_locations or []

    def validate(self, job: JobRecord) -> ValidationResult:
        reasons: list[str] = []
        if len(job.title) < 5:
            reasons.append("title_too_short")
        if len(job.title) > 120 or re.search(r"\|\s*(apply by|deadline|closing date|earn|salary)", job.title, re.I):
            reasons.append("title_not_clean")
        if not looks_like_real_role(job.title):
            reasons.append("title_not_real_role")
        if len(job.company) < 2:
            reasons.append("company_missing")
        if not looks_like_good_company(job.company):
            reasons.append("company_not_clean")
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
        haystack_full = f"{job.location}\n{job.title}\n{job.summary}"
        if (job.remote_status == "Remote" or "remote" in job.category.lower() or "remote" in job.location.lower()):
            if REMOTE_RESTRICTED_ONLY_RE.search(haystack_full) and not REMOTE_ALLOWED_RE.search(haystack_full):
                reasons.append("remote_location_restricted")
        if re.search(r"casino|betting|adult|crypto giveaway|get rich quick", job.title + " " + job.summary, re.I):
            reasons.append("low_quality_or_spam")
        if re.search(r"talent on[- ]demand|hire remote professionals on demand|browser manage security", job.summary, re.I):
            reasons.append("marketing_landing_page")
        return ValidationResult(ok=not reasons, reasons=reasons)
