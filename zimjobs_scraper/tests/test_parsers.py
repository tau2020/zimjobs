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


def test_rss_feed_parser_extracts_remote_job_item():
    from zimjobs_scraper.parsers import RssFeedParser

    cfg = SourceConfig(
        name="weworkremotely_rss",
        type="rss",
        start_urls=[],
        default_location="Remote / Worldwide",
        default_category="Remote & International",
        attribution="Attribute to WWR",
    )
    parser = RssFeedParser(cfg)
    payload = """
    <rss><channel><item>
      <title>Acme Labs: Senior Backend Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/acme-senior-backend-engineer</link>
      <pubDate>Fri, 12 Jun 2026 09:00:00 GMT</pubDate>
      <category>Anywhere in the World</category>
      <description><![CDATA[<p>Build APIs for a distributed remote team. This role is open anywhere in the world.</p>]]></description>
    </item></channel></rss>
    """
    jobs = parser.parse_listing_payload(payload, "https://weworkremotely.com/remote-jobs.rss")
    assert len(jobs) == 1
    assert jobs[0].company == "Acme Labs"
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].location == "Remote / Worldwide"
    assert jobs[0].remote_status == "Remote"


def test_jobicy_api_parser_extracts_jobs():
    from zimjobs_scraper.parsers import JobicyApiParser

    cfg = SourceConfig(name="jobicy_remote_api", type="jobicy_api", start_urls=[], default_location="Remote / Worldwide", default_category="Remote & International")
    parser = JobicyApiParser(cfg)
    payload = """{
      "jobs": [{
        "id": 123,
        "jobTitle": "Customer Support Specialist",
        "companyName": "Bright Desk",
        "jobGeo": "Anywhere",
        "jobExcerpt": "Support customers for a global SaaS platform with clear written communication and product knowledge.",
        "jobDescription": "<p>Support customers for a global SaaS platform with clear written communication and product knowledge.</p>",
        "url": "https://jobicy.com/jobs/123-customer-support-specialist",
        "pubDate": "2026-06-12"
      }]
    }"""
    jobs = parser.parse_listing_payload(payload, "https://jobicy.com/api/v2/remote-jobs")
    assert len(jobs) == 1
    assert jobs[0].title == "Customer Support Specialist"
    assert jobs[0].company == "Bright Desk"
    assert jobs[0].apply_url.startswith("https://jobicy.com/jobs/123")


def test_remoteok_api_parser_extracts_jobs():
    from zimjobs_scraper.parsers import RemoteOkApiParser

    cfg = SourceConfig(name="remoteok_api", type="remoteok_api", start_urls=[], default_location="Remote / Worldwide", default_category="Remote & International")
    parser = RemoteOkApiParser(cfg)
    payload = """[
      {"legal": "source attribution required"},
      {
        "id": 456,
        "position": "Product Manager",
        "company": "Global Tools",
        "location": "Worldwide",
        "description": "<p>Lead product delivery for a worldwide remote team serving small businesses.</p>",
        "url": "https://remoteok.com/remote-jobs/456-product-manager",
        "date": "2026-06-12T09:00:00+00:00",
        "salary_min": 40000,
        "salary_max": 80000
      }
    ]"""
    jobs = parser.parse_listing_payload(payload, "https://remoteok.com/api")
    assert len(jobs) == 1
    assert jobs[0].title == "Product Manager"
    assert jobs[0].salary_range == "USD 40000 - 80000"


def test_reliefweb_api_parser_extracts_jobs():
    from zimjobs_scraper.parsers import ReliefWebApiParser

    cfg = SourceConfig(name="reliefweb_zimbabwe_api", type="reliefweb_api", start_urls=[], default_location="Zimbabwe", default_category="NGO & Development")
    parser = ReliefWebApiParser(cfg)
    payload = """{
      "data": [{
        "id": "789",
        "fields": {
          "title": "Finance Officer",
          "source": [{"name": "World Vision"}],
          "country": [{"name": "Zimbabwe"}],
          "city": [{"name": "Harare"}],
          "body-html": "<p>Manage grant finance, donor reporting and compliance for a humanitarian programme in Harare.</p>",
          "url_alias": "/job/789/finance-officer",
          "date": {"created": "2026-06-12T00:00:00+00:00", "closing": "2026-06-30T00:00:00+00:00"}
        }
      }]
    }"""
    jobs = parser.parse_listing_payload(payload, "https://api.reliefweb.int/v2/jobs")
    assert len(jobs) == 1
    assert jobs[0].company == "World Vision"
    assert jobs[0].location == "Harare, Zimbabwe"
    assert jobs[0].expires_at == "2026-06-30"
