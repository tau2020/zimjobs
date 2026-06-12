You are a senior full-stack engineer and data extraction specialist. Build a production-ready job-board scraper and data-mapping pipeline.

Goal:
Create a scraper that can extract job-posting data from provided source URLs and map the extracted data into my existing database schema so the final job board has clean, structured, deduplicated, searchable job listings.

Inputs I will provide:
1. Source URLs or lists of source links.
2. Screenshots or copied page content from the source pages.
3. My database table names and column names.
4. Any required business rules for filtering, formatting, or categorizing jobs.

Core requirements:
- Analyze each source URL/page structure.
- Extract relevant job data from each source.
- Map extracted fields to my database columns.
- Normalize inconsistent source data.
- Handle missing, renamed, or differently formatted fields.
- Deduplicate jobs across multiple sources.
- Validate data before inserting or updating the database.
- Log extraction, mapping, validation, and insertion results.
- Make the scraper modular so new job sources can be added later.

Important compliance requirements:
- Only scrape publicly accessible job-posting data.
- Respect robots.txt, rate limits, and source website terms.
- Do not bypass login walls, CAPTCHAs, paywalls, or anti-bot protections.
- Do not collect unnecessary personal data.

Database mapping behavior:
For every source, create a mapping layer that converts source fields into my database fields.

Example mapping logic:
- Source title, job_name, position → db.jobs.title
- Source company, employer, organization → db.jobs.company_name
- Source location, city, workplace → db.jobs.location
- Source salary, pay_range, compensation → db.jobs.salary_range
- Source job_type, employment_type → db.jobs.employment_type
- Source description, details, responsibilities → db.jobs.description
- Source apply_url, application_link → db.jobs.apply_url
- Source posted_date, date_published → db.jobs.posted_at
- Source remote, workplace_type → db.jobs.remote_status
- Source source_url → db.jobs.source_url

The system should be able to infer mappings from screenshots, HTML, page text, and source examples, but it must ask for clarification when a field cannot be confidently mapped.

Technical requirements:
- Use a clean, maintainable architecture.
- Separate scraping, parsing, mapping, validation, deduplication, and database-writing logic.
- Add configuration files for each source website.
- Include retries, timeout handling, and graceful failures.
- Include structured logs.
- Include tests for parsers, mappers, and validators.
- Include documentation explaining how to add a new source.
- Use environment variables for secrets and database credentials.

Data quality requirements:
- Clean HTML from descriptions where needed.
- Preserve useful formatting in job descriptions.
- Standardize dates.
- Standardize locations.
- Normalize remote/hybrid/on-site values.
- Normalize employment type values such as full-time, part-time, contract, internship, temporary.
- Detect duplicate jobs using title, company, location, source URL, and normalized description similarity.
- Store the original source URL for traceability.

links
<links>zim jobs: https://applynow.co.zw/

remote jobs: https://somewhere.com/jobs look for emea/africa remote jobs

ngo jobs: https://www.impactpool.org/jobs/1217853



</links>


<mappingtofields>
       
        title, company,
        location , category ,
        summary , apply_url ,
        featured INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
        
        </mappingtofields>


Expected deliverables:
Working python scraper code


Before coding:
Ask me for the following missing details:
1. My database type, such as PostgreSQL, MySQL, Supabase, MongoDB, or another system.
2. My exact database schema and column names.
3. The first source URLs to support.
4. Example screenshots or copied page content.
5. Preferred programming language and framework.
6. How often the scraper should run.
7. Whether jobs should be inserted, updated, archived, or deleted when removed from the source.
8. Required filters, such as location, industry, salary, remote only, or job type.

After receiving those details:
implement the scraper :     
