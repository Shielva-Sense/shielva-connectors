# Loom Connector — Setup Guide

This guide walks you through obtaining a Loom API key and configuring the Shielva Loom connector to sync your videos, folders, and workspaces.

---

## 1. Generate a Loom API Key

1. Sign in to your Loom account at [loom.com](https://www.loom.com).
2. Click your **profile avatar** in the top-right corner and choose **Account Settings**.
3. In the left sidebar, navigate to **Integrations**.
4. Click the **API** tab (or **Developer** in some workspace plans).
5. Click **Create new API key** (or **Generate token**).
6. Give it a name (e.g. "Shielva Connector") and set the appropriate permissions:
   - **Videos — Read** (required for syncing videos and transcripts)
   - **Folders — Read** (required for folder structure)
   - **Workspaces — Read** (required for workspace info)
7. Click **Create** and immediately copy the API key — it is shown only once.

> **Workspace plans:** API access may require a Loom Business or Enterprise plan. Contact your Loom workspace admin if the API tab is not visible.

---

## 2. Understand the Workspace & Folder Structure

Loom organises content hierarchically:

```
Workspace
└── Folder (root or nested)
    └── Video
```

- A **Workspace** is the top-level org unit (typically one per company).
- **Folders** can be nested. The connector syncs all root-level and nested folders reachable via the API key.
- **Videos** belong to a folder (or are unfiled). The connector paginates all videos via the `next_page` cursor until the full library is retrieved.

The connector fetches transcripts automatically for each video where a transcript is available. If a transcript is not yet ready (video still processing or transcription not enabled), the connector falls back to the video description as content.

---

## 3. Transcript Availability

Loom generates transcripts automatically for most videos after upload completes. However:

- Videos in **processing** or **transcoding** state will not yet have a transcript. Re-run sync once the video is in **ready** state.
- Transcription must be **enabled** for your workspace. Go to **Account Settings → Transcription** to verify it is turned on.
- Transcripts are retrieved via `GET /videos/{id}/transcript`. If the endpoint returns 404, the connector falls back to the video description.

---

## 4. Rate Limits

Loom's API enforces rate limits per API key:

| Tier | Limit |
|------|-------|
| Standard | 60 requests/minute |
| Enterprise | Higher limits — contact Loom support |

The Shielva connector handles **429 Too Many Requests** responses with exponential-backoff retry (up to 3 attempts). For workspaces with thousands of videos, consider scheduling syncs during off-peak hours.

---

## 5. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **API Key** | The API key you copied from Loom → Account Settings → Integrations → API |

Click **Install**. Shielva will validate the key via `GET /me` and confirm connectivity before starting the first sync.

---

## 6. What Gets Synced

| Resource | API Endpoint | Notes |
|----------|-------------|-------|
| Videos | `GET /videos` (paginated) | Title, description, URL, duration, status, folder/workspace |
| Transcripts | `GET /videos/{id}/transcript` | Preferred content; falls back to description |
| Folders | `GET /folders` | Name, parent folder, workspace |
| Workspaces | `GET /workspaces` | Name, member count |

Each synced resource receives a **stable document ID** computed as `sha256("video:" + id)[:16]` (or `"folder:"` / `"workspace:"`), ensuring the same resource is never duplicated across incremental syncs.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `401 Unauthorized` on health check | Invalid or expired API key | Regenerate the key in Loom settings and reinstall |
| `403 Forbidden` on videos | API key lacks read permissions | Edit the key in Loom settings and add Video Read scope |
| Videos synced but no transcript content | Transcription disabled or video still processing | Enable transcription in workspace settings; wait for video to reach `ready` state |
| Sync stops mid-way | Rate limit hit | The connector will retry automatically. For large libraries, reduce sync frequency. |

---

## 8. Security Notes

- The API key is stored encrypted (AES-256-GCM) in Shielva's credential store.
- The key is transmitted exclusively via `Authorization: Bearer {api_key}` over HTTPS — never in URL parameters or request bodies.
- Rotate your Loom API key periodically and update the connector config after rotation.
