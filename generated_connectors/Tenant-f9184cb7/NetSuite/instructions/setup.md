# Setup Instructions: NetSuite

## Overview

The NetSuite connector integrates your Oracle NetSuite ERP with the Shielva platform using **Token-Based Authentication (TBA)** — NetSuite's OAuth 1.0a implementation with HMAC-SHA256 signatures. Once connected, Shielva can read and sync your customers, invoices, and items into the knowledge base, and execute arbitrary SuiteQL queries for advanced data access.

TBA uses four credential values (consumer key/secret + token key/secret) that you provision entirely within NetSuite. There is no OAuth consent redirect — credentials are copied directly from NetSuite's admin interface into Shielva.

---

## Prerequisites

Before you begin, make sure you have:

- A **NetSuite account** with administrator access (or a role that includes Setup permissions).
- The **SuiteTalk REST Web Services** feature enabled in your NetSuite account (Setup > Company > Enable Features > SuiteCloud > SuiteTalk REST Web Services).
- The **TOKEN_BASED_AUTHENTICATION** (TBA) feature enabled (Setup > Company > Enable Features > SuiteCloud > Token-Based Authentication).
- A user account in NetSuite with a role that has access to the records you want to sync (Customer, Transaction, Item).

---

## Step-by-Step Configuration

### Step 1: Account ID (`account_id`) — **Required**

Your NetSuite Account ID identifies which NetSuite instance to connect to.

1. In NetSuite, navigate to **Setup > Company > Company Information**.
2. Locate the **NetSuite Account ID** field — it is a numeric string (e.g. `1234567`).
3. For **sandbox** accounts, append the sandbox suffix (e.g. `1234567_SB1`).
4. Paste this value into the **Account ID** field in Shielva.

> **Note:** Shielva automatically converts underscores to hyphens when building the API hostname (e.g. `1234567_SB1` → `1234567-sb1.suitetalk.api.netsuite.com`).

---

### Step 2: Create an Integration Record (Consumer Key & Secret)

The Integration record is the application identity that Shielva registers in NetSuite.

1. In NetSuite, navigate to **Setup > Integration > Integration Management > New**.
2. Fill in the form:
   - **Name:** `Shielva Platform` (or any descriptive name)
   - **State:** Enabled
   - Under **Authentication**, check **Token-Based Authentication**
   - Uncheck **OAuth 2.0 Authorization Code Grant** and **TBA: Authorization Flow** (TBA credentials will be created manually)
3. Click **Save**.
4. NetSuite displays the **Consumer Key** and **Consumer Secret** exactly once — copy them immediately.
5. Paste the **Consumer Key** into the **Consumer Key** field in Shielva.
6. Paste the **Consumer Secret** into the **Consumer Secret** field in Shielva.

> **Warning:** The Consumer Secret is shown only once. If you lose it, you must create a new Integration record.

---

### Step 3: Create an Access Token (Token Key & Secret)

Access Tokens bind an Integration (the app) to a specific NetSuite user and role.

**Option A — Via Setup (recommended for admins):**

1. In NetSuite, navigate to **Setup > Users/Roles > Access Tokens > New**.
2. Select:
   - **Application Name:** the Integration record you created in Step 2
   - **User:** the NetSuite user that Shielva will act as
   - **Role:** the role that gives access to customers, transactions, and items
3. Click **Save**.
4. NetSuite displays the **Token ID** and **Token Secret** exactly once — copy them immediately.
5. Paste the **Token ID** into the **Token Key** field in Shielva.
6. Paste the **Token Secret** into the **Token Secret** field in Shielva.

**Option B — Via User Provisioning:**

1. In NetSuite, navigate to your user record.
2. Under the **Access Tokens** subtab, click **New Access Token**.
3. Follow the same steps as Option A.

> **Warning:** The Token Secret is shown only once. If you lose it, revoke the token and create a new one.

---

### Step 4: Verify Role Permissions

The NetSuite role assigned to the access token must include the following permissions:

| Permission | Level |
|---|---|
| Customers | View |
| Transactions (Invoices) | View |
| Items | View |
| SuiteQL / SuiteAnalytics Workbook | View (for suiteql() method) |
| REST Web Services | Full |

To check or update permissions:
1. Navigate to **Setup > Users/Roles > Manage Roles**.
2. Click **Edit** on the role used for the token.
3. Under the **Permissions** tab, verify the permissions above.

---

## Testing the Connection

After filling in all five fields and clicking **Install**:

1. Shielva calls `GET /record/v1/customer?limit=1` with a fresh OAuth 1.0a signature to verify the credentials.
2. If successful, the connector status shows **Connected** (green).
3. Click **Run Health Check** at any time to re-verify connectivity.
4. Click **Sync Now** to pull your first batch of customers, invoices, and items.
5. To test SuiteQL, use the **SuiteQL Query** API with a query like:
   ```sql
   SELECT id, companyName, email FROM customer WHERE isInactive = 'F'
   ```

---

## Environment: Production vs. Sandbox

| Setting | Production | Sandbox |
|---|---|---|
| Account ID format | `1234567` | `1234567_SB1` |
| API hostname | `1234567.suitetalk.api.netsuite.com` | `1234567-sb1.suitetalk.api.netsuite.com` |
| Consumer Key / Secret | Production Integration | Sandbox Integration |
| Token Key / Secret | Production token | Sandbox token |
| Data | Live ERP data | Sample / test data |

> Create a separate Integration record and Access Token for each environment — credentials do not transfer between production and sandbox.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on install | One or more TBA credentials are wrong | Double-check all four credential values against NetSuite — Consumer Key/Secret must match the Integration record, Token Key/Secret must match the Access Token |
| `403 Forbidden` on API calls | The role lacks required permissions | Review the role's permissions for Customers, Transactions, Items, and REST Web Services |
| `Invalid consumer key` in error detail | Consumer Key copied incorrectly (extra spaces, wrong env) | Re-copy from Setup > Integration > Integration Management — ensure you are in the correct environment (prod vs sandbox) |
| `Invalid signature` error | Consumer Secret or Token Secret is wrong | Re-copy both secrets; they are shown only once — if lost, revoke and create new credentials |
| `Account ID not found` / hostname resolution fails | Wrong Account ID or missing sandbox suffix | Verify under Setup > Company > Company Information; add `_SB1` for sandbox |
| `SuiteTalk REST Web Services not enabled` | Feature is disabled in your account | Go to Setup > Company > Enable Features > SuiteCloud and enable SuiteTalk REST Web Services |
| Token-Based Authentication disabled | TBA feature is off | Go to Setup > Company > Enable Features > SuiteCloud and enable Token-Based Authentication |
| Rate limit errors | NetSuite API concurrency limits exceeded | The connector retries automatically with exponential backoff; for persistent throttling, reduce sync frequency |
| Item list returns 0 results | Item record type permissions missing | Ensure the role has View access to the Item record type under Setup > Users/Roles > Manage Roles > Permissions > Lists |
| SuiteQL returns 403 | SuiteAnalytics Workbook permission missing | Add `SuiteAnalytics Workbook` at `Full` to the role, or use direct record REST endpoints instead of SuiteQL |
