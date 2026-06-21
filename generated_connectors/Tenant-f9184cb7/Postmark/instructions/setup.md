# Postmark connector — setup

## 1. Verify a sender domain (one time, per Postmark account)

Postmark refuses to send mail from any domain that isn't verified.

1. Open Postmark → **Sender Signatures** → **Add Domain**.
2. Add the SPF + DKIM records Postmark generates to your DNS provider.
3. Wait for the dashboard to mark both as **Verified**.
4. Either rely on the verified domain (e.g. `@yourdomain.com`) or add an
   individual **Sender Signature** for the exact `From` address you'll use.

## 2. Mint a Server Token (required)

1. Open Postmark → **Servers** → pick (or create) the server you want this
   connector to use.
2. Inside that server: **API Tokens** → **Create New Token** → copy the value.
3. Paste it into the connector's **Server Token** install field.

Server tokens are scoped to one Postmark server. Treat them like any other
secret — they live in `connector.json.install_fields` as `type: secret` and
are sealed before being stored.

## 3. (Optional) Mint an Account Token

Required only if you plan to call `list_servers()` or any other account-wide
endpoint. From the Postmark **Account** page → **API Tokens** → **Create New
Account-Level Token**. Paste it into the **Account Token** field.

## 4. (Optional) Set a default `From` address

If you set `default_from_email` in the install fields, every `send_email()` or
`send_with_template()` call that omits `from_email` will use that address. It
must be a verified sender or it will 422.

## 5. Rate limits

Postmark allows ~600 requests/minute per server by default — enough for most
transactional flows. The connector defaults `rate_limit_per_min` to `600`; the
HTTP client retries on `429` with exponential backoff up to three times.

## 6. Inactive recipients

When Postmark refuses delivery because a recipient is deactivated (hard bounce
or spam complaint), the API returns HTTP 422 with `ErrorCode: 406`. The
connector surfaces this as `PostmarkInactiveRecipient` so callers can decide
whether to drop, route, or re-activate via `activate_bounce()`.
