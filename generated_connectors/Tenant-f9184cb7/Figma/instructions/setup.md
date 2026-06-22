# Figma Connector — Setup Guide

## Overview

The Figma connector syncs your team's design files, projects, components, published styles, and comments into Shielva via the Figma REST API. Authentication uses a **Personal Access Token (PAT)** — no OAuth flow required.

---

## Step 1 — Create a Personal Access Token

1. Log into your Figma account at [figma.com](https://www.figma.com).
2. Click your profile picture (top-left) → **Settings**.
3. Scroll to the **Security** section.
4. Under **Personal access tokens**, click **Generate new token**.
5. Give the token a descriptive name (e.g. `Shielva Connector`).
6. Copy the token immediately — Figma will not show it again.

The token grants read access to all files, teams, and projects accessible to your account. For production use, generate the token under a dedicated service/bot Figma account.

---

## Step 2 — Find your Team ID (optional but recommended)

The Team ID unlocks team-scoped resources: projects, files, components, and styles. Without it, the connector has no entry point for discovery.

1. In Figma, navigate to a team workspace.
2. Look at the URL:
   ```
   https://www.figma.com/files/team/1234567890/My-Team
   ```
3. The numeric segment after `/team/` is your **Team ID** — `1234567890` in the example above.

---

## Step 3 — Install the connector in Shielva

In the Shielva ACP UI:

1. Go to **Connectors** → **Add Connector**.
2. Select **Figma** from the marketplace.
3. Enter the following fields:

| Field | Value | Notes |
|-------|-------|-------|
| **Personal Access Token** | `figd_xxxxx…` | Required — the PAT from Step 1 |
| **Team ID** | `1234567890` | Strongly recommended — enables full sync |

4. Click **Install**. Shielva validates that the token is present.
5. Click **Health Check** to confirm the token is valid (calls `GET /me`).

---

## How the connector syncs

With a Team ID configured, each sync run performs the following traversal:

```
GET /teams/{team_id}/projects
  └── for each project:
        GET /projects/{project_id}/files
          └── for each file:
                GET /files/{file_key}/comments

GET /teams/{team_id}/components  (cursor-paginated)
GET /teams/{team_id}/styles      (cursor-paginated)
```

Each object is normalized into a `ConnectorDocument` with a stable SHA-256 ID (16 hex chars). Re-syncing the same file produces the same ID.

**Document types produced:**

| Figma resource | `type` field |
|----------------|-------------|
| File | `design_file` |
| Project | `figma_project` |
| Component | `component` |
| Style | `figma_style` |
| Comment | `figma_comment` |
| Version | `figma_version` |

---

## API reference (methods used)

| HTTP Client Method | Endpoint | Description |
|--------------------|----------|-------------|
| `get_me()` | `GET /me` | Authenticated user — used for health check |
| `list_projects(team_id)` | `GET /teams/{team_id}/projects` | List projects in a team |
| `list_files(project_id)` | `GET /projects/{project_id}/files` | List files in a project |
| `get_file(file_key)` | `GET /files/{file_key}` | Full document tree of a file |
| `get_file_nodes(file_key, node_ids)` | `GET /files/{file_key}/nodes?ids=…` | Specific nodes within a file |
| `get_file_comments(file_key)` | `GET /files/{file_key}/comments` | Comments on a file |
| `get_file_versions(file_key)` | `GET /files/{file_key}/versions` | Version history of a file |
| `get_team_components(team_id)` | `GET /teams/{team_id}/components` | Published components (cursor-paginated) |
| `get_team_styles(team_id)` | `GET /teams/{team_id}/styles` | Published styles (cursor-paginated) |

---

## Figma project vs file hierarchy

```
Team
├── Project A
│   ├── File 1 (file_key = "aBcDeFg…")
│   └── File 2
└── Project B
    └── File 3
```

- **Team** → accessed by Team ID (from URL)
- **Project** → container for files; does not have design content itself
- **File** → the actual design document (identified by `file_key` — the alphanumeric string in the Figma URL after `/file/`)
- **Component / Style** → published from files and scoped to a team's component library

To get a file's `file_key` from a URL:
```
https://www.figma.com/file/aBcDeFgHiJkL/My-Design-System
                            ^^^^^^^^^^^^
                            This is the file_key
```

---

## Rate limits

Figma enforces approximately **100 requests per minute** per token. The connector:

- Uses exponential backoff with up to 3 retries on transient errors
- Raises `FigmaRateLimitError` on 429 responses (not retried automatically)
- Does not retry `FigmaAuthError` (401/403) or `FigmaNotFoundError` (404)

---

## Troubleshooting

### Health check fails with "Auth error"
- The PAT may have been revoked. Regenerate it in **Figma → Settings → Security**.
- The PAT may have been created under a different account than the files you are trying to access.

### Projects or files are missing from sync
- Confirm the Team ID is correct (numeric ID from the Figma URL, not the team name).
- The Figma account that owns the PAT must be a member of the team.

### Components/styles list is empty
- Published components and styles are only accessible after being published to the team's component library in Figma (right-click → Publish).
- Unpublished local components do not appear in `/teams/{id}/components`.

### 403 inside a 200 response
- This is a Figma API quirk — some endpoints return HTTP 200 with `{"status": 403, "err": "…"}` in the body when the token lacks access. The connector detects this pattern and raises `FigmaAuthError` correctly.

---

## Security notes

- The PAT (`api_key`) is stored encrypted in Shielva's credential store — never in plaintext.
- For production, use a dedicated Figma service account rather than a personal account.
- Revoke the PAT immediately from Figma Settings if the connector is decommissioned.
