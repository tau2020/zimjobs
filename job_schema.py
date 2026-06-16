from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - beautifulsoup4 is in requirements.
    BeautifulSoup = None


GOOGLE_EMPLOYMENT_TYPES = {
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "full_time": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACTOR",
    "contractor": "CONTRACTOR",
    "consultancy": "CONTRACTOR",
    "consultant": "CONTRACTOR",
    "internship": "INTERN",
    "intern": "INTERN",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
    "volunteer": "VOLUNTEER",
    "per diem": "PER_DIEM",
    "per-diem": "PER_DIEM",
    "gig": "OTHER",
    "roster": "OTHER",
}

COUNTRIES = {
    "zimbabwe": ("Zimbabwe", "ZW"),
    "south africa": ("South Africa", "ZA"),
    "united states": ("United States", "US"),
    "usa": ("United States", "US"),
    "us": ("United States", "US"),
    "united kingdom": ("United Kingdom", "GB"),
    "uk": ("United Kingdom", "GB"),
    "canada": ("Canada", "CA"),
    "kenya": ("Kenya", "KE"),
    "zambia": ("Zambia", "ZM"),
    "botswana": ("Botswana", "BW"),
}

ZIMBABWE_CITIES = {
    "harare",
    "bulawayo",
    "mutare",
    "gweru",
    "masvingo",
    "chitungwiza",
    "kwekwe",
    "kadoma",
    "victoria falls",
    "hwange",
    "marondera",
    "bindura",
    "chinhoyi",
    "kariba",
}

CURRENCY_PATTERNS = (
    ("USD", re.compile(r"\b(?:USD|US\$|US\s+dollars?)\b", re.I)),
    ("EUR", re.compile(r"\bEUR\b|\u20ac", re.I)),
    ("GBP", re.compile(r"\bGBP\b|\u00a3", re.I)),
    ("ZAR", re.compile(r"\bZAR\b", re.I)),
)


def build_job_posting_json_ld(
    job: Any,
    site_config: Mapping[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    """Build Google JobPosting JSON-LD from one normalized job row.

    Returns None when the row should not expose active JobPosting markup.
    """
    site_config = site_config or {}
    today = today or datetime.now(timezone.utc).date()
    if not is_active_job_for_schema(job, today=today):
        return None

    title = inline_text(row_get(job, "title"))
    description = description_html(job)
    date_posted = iso_date(row_get(job, "posted_at") or row_get(job, "created_at"))
    company = inline_text(row_get(job, "company"))
    if not (title and description and date_posted and company):
        return None

    schema: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": description,
        "datePosted": date_posted,
        "hiringOrganization": {
            "@type": "Organization",
            "name": "confidential" if company.lower() == "confidential" else company,
        },
    }

    identifier_value = inline_text(row_get(job, "external_job_id")) or inline_text(row_get(job, "id"))
    if identifier_value:
        schema["identifier"] = {
            "@type": "PropertyValue",
            "name": company,
            "value": identifier_value,
        }

    valid_through = iso_date(row_get(job, "expires_at"))
    if valid_through:
        schema["validThrough"] = valid_through

    employment_type = employment_type_value(row_get(job, "employment_type"))
    if employment_type:
        schema["employmentType"] = employment_type

    salary = base_salary_value(row_get(job, "salary_range"))
    if salary:
        schema["baseSalary"] = salary

    location = inline_text(row_get(job, "location"))
    if is_fully_remote(job):
        schema["jobLocationType"] = "TELECOMMUTE"
        applicant_location = applicant_location_requirements(location, site_config)
        if not applicant_location:
            return None
        schema["applicantLocationRequirements"] = applicant_location
    else:
        job_location = job_location_value(location, site_config)
        if not job_location:
            return None
        schema["jobLocation"] = job_location

    job_url = inline_text(site_config.get("job_url"))
    if job_url.startswith(("http://", "https://")):
        schema["url"] = job_url

    return omit_empty(schema)


def row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    keys = getattr(row, "keys", None)
    if callable(keys) and key in keys():
        return row[key]
    return getattr(row, key, default)


def inline_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = inline_text(value)
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def parsed_date(value: Any) -> date | None:
    iso = iso_date(value)
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_active_job_for_schema(job: Any, *, today: date | None = None) -> bool:
    today = today or datetime.now(timezone.utc).date()
    tags = {
        tag.strip().lower()
        for tag in re.split(r"[,;|]", inline_text(row_get(job, "tags")))
        if tag.strip()
    }
    if "closed" in tags:
        return False
    expires_at = parsed_date(row_get(job, "expires_at"))
    return not (expires_at and expires_at < today)


def description_html(job: Any) -> str:
    parts = [
        row_get(job, "job_description") or row_get(job, "summary"),
        row_get(job, "requirements"),
    ]
    text_parts = [strip_source_metadata(text_from_html(part)) for part in parts if inline_text(part)]
    text = "\n\n".join(part for part in text_parts if part)
    return plain_text_to_html(text)


def text_from_html(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not re.search(r"<[A-Za-z/][^>]*>", text):
        return clean_schema_text(text)
    if BeautifulSoup is None:
        return clean_schema_text(re.sub(r"<[^>]+>", " ", text))
    soup = BeautifulSoup(text, "html.parser")
    for bad in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
        bad.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "section", "article"]):
        block.insert_after("\n")
    return clean_schema_text(html.unescape(soup.get_text("\n")))


def strip_source_metadata(value: str) -> str:
    paragraphs = re.split(r"\n\s*\n", clean_schema_text(value))
    kept = []
    for paragraph in paragraphs:
        cleaned = paragraph.strip()
        if not cleaned:
            continue
        if re.match(r"^source\s*:", cleaned, re.I):
            continue
        kept.append(cleaned)
    return "\n\n".join(kept)


def plain_text_to_html(value: str) -> str:
    text = clean_schema_text(value)
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    html_parts = []
    for paragraph in paragraphs:
        escaped = html.escape(paragraph)
        escaped = re.sub(r"\n+", "<br>", escaped)
        html_parts.append(f"<p>{escaped}</p>")
    return "".join(html_parts)


def clean_schema_text(value: Any) -> str:
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = []
    seen = set()
    pending_blank = False
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if lines:
                pending_blank = True
            continue
        key = re.sub(r"\W+", " ", line.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        if pending_blank and lines and lines[-1] != "":
            lines.append("")
        lines.append(line)
        pending_blank = False
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())


def employment_type_value(value: Any) -> str | list[str] | None:
    raw = inline_text(value)
    if not raw:
        return None
    mapped = []
    for part in re.split(r"[,;/|+]|\band\b", raw, flags=re.I):
        key = inline_text(part).lower().replace("_", " ")
        if key in GOOGLE_EMPLOYMENT_TYPES and GOOGLE_EMPLOYMENT_TYPES[key] not in mapped:
            mapped.append(GOOGLE_EMPLOYMENT_TYPES[key])
    if not mapped:
        return None
    return mapped[0] if len(mapped) == 1 else mapped


def is_fully_remote(job: Any) -> bool:
    remote_status = inline_text(row_get(job, "remote_status")).lower()
    location = inline_text(row_get(job, "location")).lower()
    if remote_status == "hybrid" or "hybrid" in location:
        return False
    return remote_status == "remote" or bool(re.search(r"\b(remote|work from home|home[- ]based)\b", location))


def applicant_location_requirements(location: str, site_config: Mapping[str, Any]) -> dict[str, str] | None:
    lower = location.lower()
    if "zimbabwe" in lower or any(city in lower for city in ZIMBABWE_CITIES):
        return {"@type": "Country", "name": "Zimbabwe"}
    if "africa" in lower:
        return {"@type": "AdministrativeArea", "name": "Africa"}
    if "emea" in lower:
        return {"@type": "AdministrativeArea", "name": "EMEA"}
    for key, (name, _) in COUNTRIES.items():
        if re.search(rf"\b{re.escape(key)}\b", lower):
            return {"@type": "Country", "name": name}
    default_country = inline_text(site_config.get("default_remote_applicant_country"))
    if default_country:
        return {"@type": "Country", "name": default_country}
    return None


def job_location_value(location: str, site_config: Mapping[str, Any]) -> dict[str, Any] | None:
    clean_location = physical_location_text(location)
    if not clean_location:
        return None

    address: dict[str, Any] = {"@type": "PostalAddress"}
    country = country_from_location(clean_location)
    if country:
        _, country_code = country
        address["addressCountry"] = country_code
    else:
        default_country_code = inline_text(site_config.get("default_country_code"))
        if default_country_code and not re.search(r"\b(worldwide|global|anywhere|africa|emea)\b", clean_location, re.I):
            address["addressCountry"] = default_country_code

    locality = locality_from_location(clean_location)
    if locality:
        address["addressLocality"] = locality

    if "addressCountry" not in address and "addressLocality" not in address:
        return None
    return {"@type": "Place", "address": address}


def physical_location_text(location: str) -> str:
    text = inline_text(location)
    text = re.sub(r"\b(?:hybrid|on-site|onsite)\b", "", text, flags=re.I)
    text = re.sub(r"\bremote\b", "", text, flags=re.I)
    text = re.sub(r"\s*[/-]\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,-")
    return text


def country_from_location(location: str) -> tuple[str, str] | None:
    lower = location.lower()
    for key, value in COUNTRIES.items():
        if re.search(rf"\b{re.escape(key)}\b", lower):
            return value
    if any(city in lower for city in ZIMBABWE_CITIES):
        return COUNTRIES["zimbabwe"]
    return None


def locality_from_location(location: str) -> str | None:
    pieces = [inline_text(part) for part in re.split(r"[,/|-]", location) if inline_text(part)]
    for piece in pieces:
        lower = piece.lower()
        if lower in COUNTRIES or lower in {"worldwide", "global", "anywhere", "africa", "emea"}:
            continue
        return piece
    return inline_text(location) or None


def base_salary_value(value: Any) -> dict[str, Any] | None:
    text = inline_text(value)
    if not text:
        return None
    currency = salary_currency(text)
    unit = salary_unit(text)
    numbers = salary_numbers(text)
    if not (currency and unit and numbers):
        return None

    quantitative: dict[str, Any] = {"@type": "QuantitativeValue", "unitText": unit}
    if len(numbers) >= 2 and re.search(r"\b(?:to|through)\b|[-\u2013\u2014]", text, re.I):
        low, high = sorted(numbers[:2])
        quantitative["minValue"] = low
        quantitative["maxValue"] = high
    else:
        quantitative["value"] = numbers[0]

    return {
        "@type": "MonetaryAmount",
        "currency": currency,
        "value": quantitative,
    }


def salary_currency(text: str) -> str | None:
    for code, pattern in CURRENCY_PATTERNS:
        if pattern.search(text):
            return code
    return None


def salary_unit(text: str) -> str | None:
    lower = text.lower()
    if re.search(r"/\s*hour|per\s+hour|hourly", lower):
        return "HOUR"
    if re.search(r"/\s*day|per\s+day|daily", lower):
        return "DAY"
    if re.search(r"/\s*week|per\s+week|weekly", lower):
        return "WEEK"
    if re.search(r"/\s*month|per\s+month|monthly", lower):
        return "MONTH"
    if re.search(r"/\s*year|per\s+year|annually|annual|per\s+annum", lower):
        return "YEAR"
    return None


def salary_numbers(text: str) -> list[float | int]:
    values: list[float | int] = []
    for match in re.finditer(r"(?<!\d)(\d+(?:,\d{3})*(?:\.\d+)?)(?!\d)", text):
        raw = match.group(1).replace(",", "")
        value = float(raw)
        values.append(int(value) if value.is_integer() else value)
    return values


def omit_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: omit_empty(child)
            for key, child in value.items()
            if child not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [omit_empty(child) for child in value if child not in (None, "", [], {})]
    return value
