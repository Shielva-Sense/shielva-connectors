# Toggl Track connector — setup

The Toggl Track connector authenticates to the Toggl Track API using a personal
API token. Toggl uses HTTP Basic auth with the literal string `api_token` as the
password (`api_token:api_token`).

## 1. Find your API token

1. Sign in to <https://track.toggl.com>.
2. Click your avatar in the bottom-left → **Profile Settings**.
3. Scroll to **API Token** and click **Reset / Show** to reveal it.

The token looks like a 32-character hex string. Treat it like a password.

## 2. Install the connector

Open the Shielva ACP UI → Connectors → **Toggl Track** → Install. Fill in:

| Field                | Required | Notes                                                                                         |
| -------------------- | -------- | --------------------------------------------------------------------------------------------- |
| API Token            | yes      | Pasted from step 1. Stored encrypted at rest.                                                 |
| Default Workspace ID | no       | Numeric workspace id used when an API call does not supply one. Find it in any workspace URL. |
| API Base URL         | no       | Defaults to `https://api.track.toggl.com/api/v9`. Override only for private regions/testing.  |
| Reports API Base URL | no       | Defaults to `https://api.track.toggl.com/reports/api/v3`.                                     |
| Rate Limit (req/min) | no       | Soft client-side cap; defaults to 100. Toggl enforces ~1 req/sec server-side.                 |

Click **Install** — the connector calls `GET /me` to validate the token. A
healthy install returns *Connected to Toggl Track as `<email>`*.

## 3. Run the test suite

```bash
cd toggl_connector
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=.:../../shielva-connectors/core pytest -q
```

All HTTP calls are mocked with `respx`; no live token is required.

## 4. Capabilities

- `health_check`, `get_me`
- `list_workspaces`, `list_projects`, `create_project`
- `list_clients`, `list_tags`
- `list_time_entries`, `create_time_entry`, `stop_time_entry`,
  `get_current_time_entry`, `delete_time_entry`
- `get_summary_report` (Reports v3)
- `sync` — workspaces + projects + recent time entries into a KB

## 5. Rotating the token

If you reset the token in Toggl, the next call will return `401 Unauthorized`
and the connector health flips to `degraded` with `auth_status =
invalid_credentials`. Re-open the install screen and paste the new token —
nothing else needs to change.
