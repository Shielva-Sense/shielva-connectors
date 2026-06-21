# Snyk connector — setup

1. **Get an API token.** In Snyk, open **Account Settings → General → Auth Token**
   and copy the existing token (or click *regenerate*). Treat it as a secret —
   it grants the same access as your user.
2. **(Optional) Pick a default organization.** In Snyk, open **Settings → General**
   for the org you want to scan and copy the **Organization ID** (UUID). Paste it
   into the `default_org_id` field at install time so `sync()` knows which org to
   crawl and `test_*` calls can omit the org parameter.
3. **Install the connector** in Shielva. The installer calls `GET /self` against
   `https://api.snyk.io/rest` to validate the token. The connector reports
   `MISSING_CREDENTIALS` when the token is empty and `INVALID_CREDENTIALS` when
   Snyk returns 401/403.
4. **Run a health check** (or call `get_user()`) to confirm `HEALTHY`.
5. **Trigger a sync** once a `default_org_id` is set — the connector will page
   through projects and issues for that org and ingest them as
   `NormalizedDocument`s into the configured KB.

## Rate limits & retries

The HTTP client retries 429 and 5xx responses with exponential backoff
(honoring `Retry-After` when present). Default budget is 3 retries per call.
`rate_limit_per_min` is metadata for the gateway; the connector itself does not
queue requests.

## Notes

- REST endpoints (orgs, projects, issues, targets, users) use
  `Content-Type: application/vnd.api+json` and require the
  `version=YYYY-MM-DD` query parameter. The connector defaults this to
  `2024-10-15`; override with the `api_version` install field.
- Legacy v1 endpoints (`/test/npm/*`, `/test/pip/*`, `/org/*/dependencies`,
  `/org/*/integrations`, `/org/*/integrations/*/import`) use plain
  `application/json` and live under `https://api.snyk.io/v1`.
