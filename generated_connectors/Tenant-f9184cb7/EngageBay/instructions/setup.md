# EngageBay Connector — Setup Instructions

The EngageBay connector reads and writes contacts, companies, deals, tasks,
tickets, and notes in your EngageBay CRM via the documented REST API at
`https://app.engagebay.com/dev/api/panel`.

## 1. Get your REST API key

1. Log into EngageBay as an account owner or admin.
2. Click your avatar (top-right) and open **Account Settings**.
3. In the left nav, click **API Settings** → **REST API**.
4. Copy the **REST API Key** shown. Keep it secret — it grants full API access
   to your account.

> EngageBay's REST contract sends the key in the `Authorization` header
> **without** a `Bearer ` prefix. The connector handles that for you.

## 2. Install the connector

1. In Shielva → **Connectors** → **Browse**, pick **EngageBay**.
2. Paste the **REST API Key** into the `api_key` field.
3. Leave `base_url` blank unless EngageBay support has given you a non-default
   API host (defaults to `https://app.engagebay.com/dev/api/panel`).
4. Leave `rate_limit_per_min` at `60` unless your EngageBay plan documents a
   different per-minute quota.
5. Click **Install**. The connector calls `GET /subusers/list` against your
   account to validate the key. A green "Authenticated" status means you're
   ready.

## 3. What the connector can do

| Capability        | Method               | EngageBay endpoint                     |
|-------------------|----------------------|----------------------------------------|
| Health probe      | `health_check`       | `GET /subusers/list`                   |
| List contacts     | `list_contacts`      | `GET /contacts`                        |
| Read one contact  | `get_contact`        | `GET /contacts/{id}`                   |
| Create contact    | `create_contact`     | `POST /contacts`                       |
| Update contact    | `update_contact`     | `PUT /contacts/update-partial/{id}`    |
| Delete contact    | `delete_contact`     | `DELETE /contacts/{id}`                |
| List companies    | `list_companies`     | `GET /companies/list/{page_size}`      |
| List deals        | `list_deals`         | `GET /deals`                           |
| Create deal       | `create_deal`        | `POST /deals`                          |
| List tasks        | `list_tasks`         | `GET /tasks`                           |
| Create task       | `create_task`        | `POST /tasks`                          |
| List tickets      | `list_tickets`       | `GET /tickets`                         |
| Add note          | `add_note`           | `POST /contacts/{id}/note`             |
| Sync to KB        | `sync`               | walks `/contacts` cursor pages         |

## 4. Properties payload for create_contact / update_contact

EngageBay represents every contact field as an entry in a `properties` list.
The connector takes that same shape:

```python
await conn.create_contact(properties=[
    {"name": "email",      "value": "ada@example.com", "field_type": "TEXT"},
    {"name": "first_name", "value": "Ada",             "field_type": "TEXT"},
    {"name": "last_name",  "value": "Lovelace",        "field_type": "TEXT"},
    {"name": "phone",      "value": "+15555550100",    "field_type": "TEXT"},
])
```

`field_type` defaults to `"TEXT"` when omitted.

## 5. Rotating the key

If you regenerate the EngageBay REST API key, open the connector in Shielva,
paste the new key into `api_key`, and click **Reinstall**. No re-sync needed —
the connector keeps the same `connector_id` and KB documents.

## 6. Troubleshooting

| Symptom                               | Likely cause                         | Fix                                                  |
|---------------------------------------|--------------------------------------|------------------------------------------------------|
| `INVALID_CREDENTIALS` at install      | Wrong key or revoked key             | Re-copy from EngageBay → API Settings                |
| `TOKEN_EXPIRED` at health-check       | Account suspended / key rotated      | Reinstall with the current key                       |
| Repeated `429` retries                | Hitting EngageBay's per-minute quota | Lower `rate_limit_per_min`, batch fewer sync workers |
| `404` on `update_contact`             | Stale `contact_id`                   | Re-list contacts, use the freshly-returned id        |
