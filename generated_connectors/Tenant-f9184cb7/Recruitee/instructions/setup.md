# Recruitee Connector — Setup Guide

The Recruitee connector lets Shielva read and modify candidates, job offers,
pipeline stages, notes, talent pools, and departments in your Recruitee
account using a **Personal API Token**.

## 1. Find your Company ID

Log into Recruitee at `https://app.recruitee.com`. Once on your dashboard
the URL contains your Company ID, e.g.:

```
https://app.recruitee.com/#/companies/12345/dashboard
                                          ^^^^^
                                          company_id
```

Alternatively, run `GET https://api.recruitee.com/c/me` with your token and
read the `company_id` field from the response.

## 2. Generate a Personal API Token

1. In Recruitee, click your avatar → **Settings**.
2. Open **Apps & Plugins** → **Personal API Tokens**.
3. Click **New token**, give it a descriptive name
   (e.g. *Shielva Integration*), and copy the token shown **once** — it will
   not be displayed again.
4. Make sure the user who owns the token has the permissions you need
   (Administrator for full CRUD, Recruiter for most candidate operations).

## 3. Install the connector in Shielva

In Shielva, open **Connectors → Add Connector → Recruitee** and fill in:

| Field | Value |
|---|---|
| Company ID | The numeric ID from step 1 |
| Personal API Token | The token from step 2 |
| API Base URL | leave as default (`https://api.recruitee.com/c`) |
| Rate Limit (requests/min) | `60` is safe; raise if your plan allows it |

Click **Install**. The connector calls `GET /current_user` to verify the
token; on success it transitions to `connected` / `healthy`.

## 4. Available actions

| Method | Description |
|---|---|
| `list_candidates(query, sort, scope, status, limit, offset)` | Search candidates |
| `get_candidate(id)` | Full candidate record |
| `create_candidate(name, emails, phones, source, offers)` | New candidate (optionally placed) |
| `update_candidate(id, fields)` | Partial update |
| `delete_candidate(id)` | Hard delete |
| `move_candidate_stage(id, offer_id, stage_id)` | Move along pipeline |
| `list_offers(status, scope)` | Job offers / requisitions |
| `get_offer(id)` | Full offer record |
| `create_offer(title, position_type, ...)` | New requisition |
| `list_pipeline_stages(offer_id?)` | Per-offer or company templates |
| `add_note(candidate_id, body, visible_to_team_id?)` | Add a candidate note |
| `list_pools()` | Talent pools |
| `list_departments()` | Departments |

## 5. Troubleshooting

- **401 Unauthorized** — The token was revoked, expired, or copied wrong.
  Regenerate at *Settings → Apps & Plugins → Personal API Tokens*.
- **404 Not Found on /current_user** — The Company ID is wrong; recheck
  step 1.
- **429 Too Many Requests** — The connector retries automatically with
  exponential backoff and honours the `Retry-After` header. If you see
  sustained 429s, lower `rate_limit_per_min` or split workloads.
- **Empty `emails`/`phones`** — Recruitee returns these as `[{"normalized": "..."}]`
  objects; `normalize_candidate()` flattens them to plain strings.
