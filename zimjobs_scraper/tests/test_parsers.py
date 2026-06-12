from pathlib import Path

from zimjobs_scraper.parsers import ApplyNowParser, ImpactPoolParser, SourceConfig

FIXTURES = Path(__file__).parent / "fixtures"


def test_applynow_detail_parser_extracts_core_fields():
    cfg = SourceConfig(name="applynow_zimbabwe", type="applynow", start_urls=[], default_location="Zimbabwe")
    parser = ApplyNowParser(cfg)
    html = (FIXTURES / "applynow_detail.html").read_text()
    raw = parser.parse_detail(html, "https://applynow.co.zw/2026/06/01/human-rights-funders-network/")
    assert raw is not None
    assert "Events Project Manager" in raw.title
    assert raw.location.startswith("Flexible Global Location")
    assert raw.expires_at == "2026-06-22"
    assert raw.apply_url == "https://example.org/apply"
    assert "65,000" in raw.salary_range


def test_impactpool_detail_parser_extracts_core_fields():
    cfg = SourceConfig(name="impactpool_zimbabwe", type="impactpool", start_urls=[], default_location="Zimbabwe")
    parser = ImpactPoolParser(cfg)
    html = (FIXTURES / "impactpool_detail.html").read_text()
    raw = parser.parse_detail(html, "https://www.impactpool.org/jobs/1217853")
    assert raw is not None
    assert raw.title == "National Consultants and Field Coordinators Zimbabwe(Roster)"
    assert "Bodhi" in raw.company
    assert raw.apply_url == "https://www.bodhiglobalanalysis.com/jobs"
