# WordPress Connector — Setup Guide

## Overview

This connector syncs **posts, pages, users, media, categories, and tags** from your WordPress site into Shielva using the WordPress REST API v2. Authentication uses **Application Passwords** — a built-in WordPress feature available since WordPress 5.6.

---

## Prerequisites

- WordPress **5.6 or later** (Application Passwords were introduced in 5.6)
- Your WordPress user account must have sufficient permissions to read posts, pages, users, and media
- The WordPress REST API must be enabled (it is enabled by default)
- **Pretty permalinks** must be configured (Settings → Permalinks → any option except "Plain")

---

## Step 1 — Generate an Application Password

1. Log in to your WordPress Admin dashboard (`https://yourblog.com/wp-admin`)
2. Navigate to **Users → Profile** (or **Users → All Users**, then click your username)
3. Scroll down to the **Application Passwords** section
4. In the **New Application Password Name** field, enter a descriptive name (e.g. `Shielva Connector`)
5. Click **Add New Application Password**
6. WordPress will display the generated password **once** — copy it immediately. It will look like:
   ```
   xxxx xxxx xxxx xxxx xxxx xxxx
   ```
   (24 characters in groups of 4, separated by spaces)
7. Store the password securely — you cannot retrieve it again from WordPress

> **Note:** Application Passwords include spaces. Keep the spaces when entering the password into Shielva — they are part of the credential.

---

## Step 2 — Find Your Site URL

Your **WordPress Site URL** is the public URL of your WordPress installation, e.g.:
- `https://myblog.com`
- `https://company.example.com/blog`

Do not include a trailing slash. Do not include `/wp-admin` or `/wp-json`.

You can confirm the URL in **Settings → General → WordPress Address (URL)**.

---

## Step 3 — Configure the Shielva Connector

In the Shielva connector setup form, enter:

| Field | Value |
|---|---|
| **WordPress Site URL** | `https://yourblog.com` |
| **WordPress Username** | Your WordPress login username |
| **Application Password** | The password generated in Step 1 |

---

## Step 4 — Verify the Connection

Click **Test Connection** (or **Install**). Shielva calls `GET /wp-json/wp/v2/users/me` using HTTP Basic Auth with your credentials. A successful response confirms:
- The site URL is reachable
- The username and Application Password are correct
- The authenticated user has REST API access

---

## Troubleshooting

### "Authentication failed (401): Sorry, you are not allowed to do that"

- Verify the username is correct (it is your WordPress login name, not your display name)
- Re-generate the Application Password — copy it again carefully, including all spaces
- Ensure your WordPress user role has `read` permissions

### "REST API disabled" or connection timeout

The WordPress REST API may be disabled by a plugin or theme. To verify:

1. Visit `https://yourblog.com/wp-json/wp/v2/` in your browser
2. If you see a JSON response, the REST API is working
3. If you see a 404 or redirect, check:
   - **Settings → Permalinks**: must not be set to "Plain". After changing, click **Save Changes** to flush rewrite rules
   - Plugins: REST API blocking plugins (e.g. Disable REST API) may need to be deactivated or configured to allow access
   - Security plugins (Wordfence, iThemes Security) may block REST API requests — add your Shielva connector IP to the allowlist

### "404 Not Found" on /wp-json/wp/v2/

This typically means pretty permalinks are set to "Plain":
1. Go to **Settings → Permalinks**
2. Select any option other than "Plain" (e.g. "Post name")
3. Click **Save Changes**
4. Retry the connection

### Application Passwords section not visible

- Application Passwords require HTTPS. If your site runs on HTTP, the section may be hidden. Enable HTTPS (or add the filter `add_filter('wp_is_application_passwords_available', '__return_true')` to `functions.php` for development).
- Some security plugins disable Application Passwords. Check your security plugin settings.

### My user has limited access

Application Passwords inherit the permissions of the WordPress user account. If the connector cannot read certain resources (e.g. users list requires `list_users` capability), you may need to:
- Use an Administrator account to generate the Application Password
- Or install a capability-extension plugin to grant specific REST API permissions to lower-privileged roles

---

## What Gets Synced

| Resource | WordPress REST Endpoint | Notes |
|---|---|---|
| Posts | `/wp-json/wp/v2/posts` | All statuses (publish, draft, private) |
| Pages | `/wp-json/wp/v2/pages` | All statuses |
| Users | `/wp-json/wp/v2/users` | Requires `list_users` capability (admin) |
| Media | `/wp-json/wp/v2/media` | Images, videos, PDFs, etc. |
| Categories | `/wp-json/wp/v2/categories` | All categories |
| Tags | `/wp-json/wp/v2/tags` | All tags |

---

## Security Notes

- Application Passwords are stored encrypted by Shielva using AES-256-GCM
- You can revoke any Application Password at any time from WordPress Admin → Users → Profile → Application Passwords
- Each Shielva connector should use its own dedicated Application Password — do not share passwords across integrations
- Rotate Application Passwords periodically for security hygiene
