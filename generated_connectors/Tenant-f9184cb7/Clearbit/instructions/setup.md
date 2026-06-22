# Clearbit Connector — Setup Guide

## Overview

The Clearbit connector integrates with the Clearbit B2B data enrichment API. It provides on-demand enrichment for companies (by domain) and people (by email), along with combined person + company lookups.

**Important:** Clearbit is a **lookup-based API**, not a list API. There is no bulk "export all companies" endpoint. Data is retrieved on demand using `enrich_company()`, `enrich_person()`, or `combined_lookup()`. The `sync()` method always returns 0 documents — this is by design.

---

## Prerequisites

- A Clearbit account with an active API key
- Python 3.10+ environment
- `aiohttp>=3.9.0`

---

## Installation

```bash
pip install aiohttp>=3.9.0
```

---

## Getting Your Clearbit API Key

1. Log in to your Clearbit account at [https://dashboard.clearbit.com](https://dashboard.clearbit.com)
2. Navigate to **Settings → API Keys**
3. Copy your **Secret API Key** (begins with `sk-`)

---

## Configuration

The connector requires one install field:

| Field | Key | Type | Required | Description |
|-------|-----|------|----------|-------------|
| API Key | `api_key` | password | Yes | Your Clearbit secret API key |

### Auth mechanism

Clearbit uses HTTP Basic Auth with the **API key as the username and an empty password**:

```
Authorization: Basic base64(api_key:)
```

This is handled automatically by `aiohttp.BasicAuth(api_key, "")`.

---

## API Endpoints

| Method | Clearbit Endpoint | Description |
|--------|-------------------|-------------|
| `enrich_company(domain)` | `GET https://company.clearbit.com/v2/companies/find?domain={domain}` | Company enrichment by domain |
| `enrich_person(email)` | `GET https://person.clearbit.com/v2/people/find?email={email}` | Person enrichment by email |
| `combined_lookup(email)` | `GET https://person.clearbit.com/v2/combined/find?email={email}` | Person + Company in one call |
| `search_companies(query)` | `GET https://autocomplete.clearbit.com/v1/companies/suggest?query={query}` | Company autocomplete (no auth) |
| `reveal_ip(ip)` | `GET https://reveal.clearbit.com/v1/companies/find?ip={ip}` | Company reveal from IP address |

---

## Document ID Stability

All normalized documents use deterministic SHA-256 IDs for upsert deduplication:

| Document Type | ID Formula |
|---------------|------------|
| Company | `SHA-256("company:" + domain)[:16]` |
| Person | `SHA-256("person:" + email)[:16]` |
| Combined | `SHA-256("combined:" + email)[:16]` |

---

## Normalized Document Fields

### Company (from `enrich_company`)

```json
{
  "source_id": "<sha256[:16]>",
  "title": "Company: Clearbit",
  "content": "Company: Clearbit\nDomain: clearbit.com\nIndustry: Software\nLocation: San Francisco, United States",
  "metadata": {
    "type": "company",
    "name": "Clearbit",
    "domain": "clearbit.com",
    "industry": "Software",
    "location": "San Francisco, United States",
    "description": "...",
    "employees": 250,
    "founded_year": 2012
  }
}
```

### Person (from `enrich_person`)

```json
{
  "source_id": "<sha256[:16]>",
  "title": "Person: Alex Johnson",
  "content": "Person: Alex Johnson\nEmail: alex@stripe.com\nTitle: Software Engineer\nCompany: Stripe",
  "metadata": {
    "type": "person",
    "name": "Alex Johnson",
    "email": "alex@stripe.com",
    "title": "Software Engineer",
    "company": "Stripe"
  }
}
```

---

## Error Handling

| Clearbit Response | Exception Raised |
|-------------------|-----------------|
| 401 Unauthorized | `ClearbitAuthError` |
| 403 Forbidden | `ClearbitAuthError` |
| 404 Not Found | `ClearbitNotFoundError` |
| 202 Accepted (pending enrichment) | `ClearbitNotFoundError` |
| 422 Validation Error | `ClearbitError` |
| 429 Too Many Requests | `ClearbitRateLimitError` |
| 5xx Server Error | `ClearbitError` |
| Network timeout/connection failure | `ClearbitNetworkError` |

### Retry Behavior

`with_retry()` uses exponential backoff (base 1s, factor 2, max 30s, max 3 attempts):
- **Auth errors** (`ClearbitAuthError`) — never retried
- **Rate limit** (`ClearbitRateLimitError`) — retried, honouring `Retry-After` header
- **Network errors** and all other `ClearbitError` subclasses — retried up to max_attempts
- **202 Pending** — retried (data may become available momentarily)

---

## Running Tests

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/clearbit_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
```

All 105 tests must pass before registering the connector. No live credentials are required — all HTTP calls are mocked.

---

## Registering with Shielva ACP

```bash
curl -sk -X POST "https://localhost:8055/sessions/import-existing" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: Tenant-f9184cb7" \
  -H "X-App-ID: 91f5d9b2486a3610" \
  -d '{
    "connectors": [{
      "service_slug": "clearbit",
      "provider": "Clearbit",
      "service": "Clearbit",
      "connector_name": "Clearbit",
      "version": "1.0.0",
      "run_kind": "build",
      "output_dir": "/Users/vivekvarshavaishvik/Documents/client_dir/clearbit_connector"
    }]
  }' | python3 -m json.tool
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `api_key is required` | Missing API key in config | Add `api_key` to the install form |
| `401 Unauthorized` | Invalid API key | Verify the key in Clearbit → Settings → API Keys |
| `404 Not Found` for a domain/email | No Clearbit data for that entity | Try a different domain; Clearbit doesn't have every company/person |
| `202 Accepted` (pending) | Clearbit is building enrichment asynchronously | Retry after a few seconds |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically; consider spacing requests |
