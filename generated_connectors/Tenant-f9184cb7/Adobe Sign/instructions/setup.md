# Adobe Sign connector — setup

The Shielva Adobe Sign connector wraps the Adobe Acrobat Sign REST v6 API with
OAuth2 (authorization code grant). Adobe Sign is sharded — every account lives
in exactly one data center (`na1`, `eu1`, `jp1`, `in1`, `au1`, etc) and you
**must** make API calls against that data center, not the one you minted the
token from. The connector handles this automatically by reading
`api_access_point` from the OAuth2 response and pivoting its base URL.

## 1. Register an OAuth2 application

1. Go to the [Adobe Developer Console](https://developer.adobe.com/console).
2. Create a new project → **Add API** → **Adobe Sign API**.
3. Choose **OAuth Web App** as the authentication type.
4. Add a **redirect URI** that matches the one your Shielva gateway sends in
   the `redirect_uri` query parameter (the gateway sets this when it builds
   the authorize URL — copy the exact value).
5. Note the **Client ID** and **Client Secret**.

## 2. Choose your shard

The default is `na1` (North America). If you log into Adobe Sign and your
URL bar shows `https://secure.eu1.adobesign.com/...`, your shard is `eu1`.
Supply the lower-case shard code in the `shard` install field. The connector
also accepts an `api_access_point` returned during OAuth and will switch to
the correct shard for you — but a wrong initial shard will fail the consent
screen because the authorize URL is shard-specific.

## 3. Required scopes

```
user_read user_write
agreement_read agreement_write agreement_send
library_read library_write
workflow_read workflow_write
```

These cover every method exposed by the connector. Strip any you do not need
before installing — Adobe Sign honors a least-privilege scope set.

## 4. Install fields (recap)

| key | required | default | notes |
|-----|----------|---------|-------|
| `client_id` | yes | — | From Adobe Developer Console |
| `client_secret` | yes | — | From Adobe Developer Console |
| `shard` | no | `na1` | Your Adobe Sign data center |
| `scopes` | no | (see above) | Space-separated |
| `auth_url` | no | shard default | Override if you operate a proxy |
| `token_url` | no | shard default | Override if you operate a proxy |
| `rate_limit_per_min` | no | `60` | Throttle ceiling |

## 5. Verify the install

After OAuth, call `health_check()`. A `HEALTHY/CONNECTED` response confirms
the token works and the connector is pointing at the right shard.

## 6. Common pitfalls

- **404 on every call after a successful token exchange**: the connector
  could not parse `api_access_point` from the OAuth response. Confirm your
  `shard` install value matches the tenant's data center.
- **`AdobeSignAuthError` on every call**: the refresh token has been revoked.
  Re-run the OAuth flow.
- **Transient document upload 400**: Adobe Sign rejects files > 10 MB and
  any MIME type outside `application/pdf` / `application/msword` /
  `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
  unless your account has the relevant feature enabled.
