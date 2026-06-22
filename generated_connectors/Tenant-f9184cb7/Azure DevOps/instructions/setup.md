# Azure DevOps connector — setup

This connector talks to Azure DevOps Services (`dev.azure.com`) via the REST
API using a Personal Access Token (PAT) sent as HTTP Basic auth (empty
username, PAT as password).

## 1. Create a Personal Access Token

1. Sign in to `https://dev.azure.com/{your-organization}`.
2. Top-right user menu → **User settings** → **Personal access tokens**.
3. **New Token**:
   - **Name** — `Shielva connector` (or anything descriptive).
   - **Organization** — pick the org Shielva will sync from.
   - **Expiration** — at least 90 days; rotate before expiry.
   - **Scopes** — choose **Custom defined** and enable:
     - `Code` → **Read & write** (list repos, list/create pull requests).
     - `Work Items` → **Read, write, & manage** (WIQL queries + create/update).
     - `Build` → **Read & execute** (list/queue builds, list pipelines).
4. Copy the token *immediately* — Azure DevOps shows it only once.

## 2. Install the connector

In the Shielva platform UI → Connectors → **Azure DevOps** → Install.

Provide:

| Field                    | Required | Example                               |
| ------------------------ | -------- | ------------------------------------- |
| `organization`           | yes      | `shielva-ai`                          |
| `personal_access_token`  | yes      | `q7…` (paste from step 1)             |
| `api_version`            | no       | `7.1` (default)                       |
| `default_project`        | no       | `Shielva` (used by `sync()`)          |
| `rate_limit_per_min`     | no       | `200` (default)                       |

The connector validates the input and persists it via sealed config. The PAT
itself is **never** logged.

## 3. APIs surfaced

| Method                | Endpoint                                                                          | Scope needed       |
| --------------------- | --------------------------------------------------------------------------------- | ------------------ |
| `health_check`        | `GET /_apis/projects?$top=1`                                                      | any                |
| `list_projects`       | `GET /_apis/projects`                                                              | any                |
| `get_project`         | `GET /_apis/projects/{id}`                                                         | any                |
| `list_repos`          | `GET /{project}/_apis/git/repositories`                                            | Code (read)        |
| `get_repo`            | `GET /{project}/_apis/git/repositories/{id}`                                       | Code (read)        |
| `list_pull_requests`  | `GET /{project}/_apis/git/repositories/{rid}/pullrequests`                         | Code (read)        |
| `create_pull_request` | `POST /{project}/_apis/git/repositories/{rid}/pullrequests`                        | Code (read & write)|
| `list_work_items`     | `POST /{project}/_apis/wit/wiql` → `GET /_apis/wit/workitems?ids=…`                | Work Items (read)  |
| `get_work_item`       | `GET /_apis/wit/workitems/{id}`                                                    | Work Items (read)  |
| `create_work_item`    | `POST /{project}/_apis/wit/workitems/${type}` (`application/json-patch+json`)      | Work Items (write) |
| `update_work_item`    | `PATCH /_apis/wit/workitems/{id}` (`application/json-patch+json`)                  | Work Items (write) |
| `list_builds`         | `GET /{project}/_apis/build/builds`                                                | Build (read)       |
| `queue_build`         | `POST /{project}/_apis/build/builds`                                               | Build (read & execute)|
| `list_pipelines`      | `GET /{project}/_apis/pipelines`                                                   | Build (read)       |

All requests carry `Accept: application/json;api-version=7.1` and the default
`api-version=7.1` query parameter unless overridden at install time.

## 4. Rotating the PAT

1. Issue a new PAT with the same scopes (above) before the old one expires.
2. In the Shielva UI → Connectors → Azure DevOps → **Reconfigure** → paste the
   new PAT.
3. The next `health_check` call confirms the new credential is live.

## 5. Troubleshooting

- **`401 Unauthorized`** — PAT expired or wrong organization. Re-issue and
  reinstall.
- **`403 Forbidden`** — PAT scope insufficient for the requested API. Re-issue
  with the matching scope from the table above.
- **`404 Not Found`** — project, repo, or work item ID does not exist in the
  organization. Verify the resource path.
- **Repeated `429`** — increase `rate_limit_per_min` only after confirming
  the org-level Azure DevOps throttle allows it.
