# Setup Instructions: Firebase

## Overview

The Firebase connector integrates Google Firebase with the Shielva platform.
It wraps four Firebase surfaces behind a single async API:

- **Firestore** — document CRUD and structured queries
- **Realtime Database (RTDB)** — JSON tree at a `database_url`
- **Cloud Messaging (FCM v1)** — server-to-device push
- **Firebase Auth (Identity Toolkit)** — admin user lookup + create

Authentication uses a Google **service-account JSON**. The connector signs a
short-lived JWT with the private key and exchanges it for an OAuth2 access
token at `https://oauth2.googleapis.com/token`. No password is ever shared
with Shielva.

---

## Prerequisites

- A **Firebase project** (create one at <https://console.firebase.google.com>).
- The **Firestore**, **Realtime Database**, **Cloud Messaging**, and/or
  **Authentication** products enabled — only the ones you plan to use.
- A **service-account JSON** with the Firebase Admin SDK role. The connector
  needs the following scopes (the SDK role grants all of them):
  - `https://www.googleapis.com/auth/datastore`
  - `https://www.googleapis.com/auth/firebase.database`
  - `https://www.googleapis.com/auth/firebase.messaging`
  - `https://www.googleapis.com/auth/identitytoolkit`

---

## Step 1 — Firebase Project ID (`project_id`) — **Required**

1. Open <https://console.firebase.google.com> and select your project.
2. Click the gear icon (top-left) → **Project settings**.
3. On the **General** tab, copy the **Project ID** (lowercase, hyphenated;
   e.g. `my-firebase-project`).
4. Paste it into the **Firebase Project ID** field in Shielva.

---

## Step 2 — Service Account JSON (`service_account_json`) — **Required**

1. In Project settings, open the **Service accounts** tab.
2. Click **Generate new private key** → **Generate key**.
3. A JSON file downloads. Open it in a text editor.
4. Copy the **entire** JSON contents (from the opening `{` to the closing `}`)
   and paste it into the **Service Account JSON** field in Shielva.

> The connector validates the JSON at install time. If the file is malformed
> or missing `client_email` / `private_key`, install fails with
> `MISSING_CREDENTIALS`.

Treat this JSON like a password — anyone with it can act as the service
account. Delete the local download after pasting.

---

## Step 3 — Realtime Database URL (`database_url`) — **Optional**

If you plan to use the `rtdb_*` methods:

1. In the Firebase console sidebar, open **Realtime Database**.
2. At the top of the data view, copy the URL — for example
   `https://my-project-default-rtdb.firebaseio.com` or
   `https://my-project-default-rtdb.europe-west1.firebasedatabase.app`.
3. Paste it into the **Realtime Database URL** field.

Leave this field blank if you only use Firestore / FCM / Auth.

---

## Step 4 — Rate Limit (`rate_limit_per_min`) — **Optional**

Default: `600` requests per minute. Firebase's own quotas are far higher;
this knob lets you cap the connector before hitting Firebase's limits in
shared-quota deployments.

---

## Step 5 — Verify the Connection

After saving, click **Health check**. Shielva will:

1. Sign a JWT with the service-account private key.
2. Exchange it at `https://oauth2.googleapis.com/token` for an access token.
3. Probe Firestore for a sentinel collection.

A green **Connected** status confirms credentials work. Common failures:

- **Token expired** → the service-account JSON is malformed or revoked.
  Regenerate the key in Firebase console and re-paste.
- **Offline (network error)** → the host running Shielva cannot reach
  `oauth2.googleapis.com` or `firestore.googleapis.com`. Check egress rules.

---

## Method Reference (summary)

- `firestore_get_document(collection, document_id)`
- `firestore_set_document(collection, document_id, fields)`
- `firestore_create_document(collection, fields, document_id=None)`
- `firestore_delete_document(collection, document_id)`
- `firestore_list_documents(collection, page_size=100, page_token=None)`
- `firestore_query(collection, where=[...], order_by=[...], limit=N)`
- `rtdb_get(path)` / `rtdb_set(path, data)` / `rtdb_update(path, data)` / `rtdb_delete(path)`
- `fcm_send(token=..., topic=..., notification=..., data=..., android=..., apns=...)`
- `auth_get_user(uid)`
- `auth_create_user(email, password=None, display_name=None, phone_number=None)`

All methods are `async` and return raw API response dicts.
