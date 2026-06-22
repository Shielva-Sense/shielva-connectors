# Discord Connector — Setup

This connector talks to the Discord REST API (v10) using either an OAuth2 user
token *or* a Bot token. Pick the path that matches your use case.

## Path A — OAuth2 (act as a Discord user)

Use when the connector must act on behalf of an end-user (read their guilds,
read DMs they granted, etc.).

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create (or pick) an application.
3. Navigate to **OAuth2 → General** and copy:
   - **Client ID** → paste into the install field `client_id`.
   - **Client Secret** (click "Reset Secret" if you don't have it) → paste into `client_secret`.
4. Under **OAuth2 → Redirects**, add the Shielva callback URL the platform shows
   you on the install screen (e.g. `https://your-shielva.example.com/connectors/discord/callback`).
5. Set the desired scopes in `scopes`. Defaults: `identify guilds messages.read`.
   Add `guilds.members.read` if you need `list_guild_members`.
6. Click **Install** in Shielva, then **Authorize** to walk through the Discord
   consent screen.

## Path B — Bot token (act as a bot)

Use when the connector should post and react as a bot in servers it has been
invited to.

1. In the Discord Developer Portal, open your application and navigate to **Bot**.
2. Click **Reset Token** to mint a fresh token and copy it.
3. Paste the token into the `bot_token` install field. Leave `client_id` /
   `client_secret` blank (or fill them in if you also want OAuth — `bot_token`
   takes precedence at runtime).
4. Invite the bot to the target server(s):
   - **OAuth2 → URL Generator** → check the `bot` scope.
   - Pick the bot permissions you need (e.g. `Send Messages`, `Read Message History`,
     `Add Reactions`).
   - Visit the generated URL and pick a server.
5. Click **Install** in Shielva — the connector becomes AUTHENTICATED immediately.

## Verifying

After install:

- The platform's **Health Check** action issues `GET /users/@me`. A HEALTHY
  result confirms the credentials are valid.
- A 401 means the OAuth token expired (the connector will auto-refresh on the
  next call) or the bot token was rotated.

## Rate limits

Discord enforces per-route token-bucket limits. The connector:

1. Reads `X-RateLimit-Remaining` and `X-RateLimit-Reset-After` headers on every
   response. When `Remaining == 0`, it sleeps until reset before the next call.
2. On a 429 response, parses the JSON body's `retry_after` (seconds) and waits
   exactly that long before retrying.

Set `rate_limit_per_min` only as a soft client-side hint; Discord's server-side
buckets are the authoritative limit.

## Scopes reference

| Scope                    | Why                                                    |
|--------------------------|--------------------------------------------------------|
| `identify`               | `GET /users/@me`                                       |
| `guilds`                 | `GET /users/@me/guilds`                                |
| `messages.read`          | Read DM channel messages                               |
| `guilds.members.read`    | `GET /guilds/{id}/members`                             |
| `bot`                    | Bot install (use with Bot token path)                  |

## Troubleshooting

- **`MISSING_CREDENTIALS`** — neither `bot_token` nor (`client_id` + `client_secret`) is set.
- **`TOKEN_EXPIRED`** — the stored access token expired and the refresh token was
  rejected. Re-authorize from the Shielva connectors UI.
- **404 from `get_message` / `get_channel`** — the bot/user is not a member of
  the guild that owns the channel, or the message/channel ID is wrong.
