# Pocket Relay relay

A Cloudflare Worker with two jobs.

**Remote access tunnel** (`src/tunnel.js`). A bridge connects to `/v1/tunnel/<room>/host` and its paired phones to `/v1/tunnel/<room>/client`. The worker passes sealed records between them. They are encrypted end to end with a key the worker never sees, and nothing is stored. Only the bridge that created a room can be its host: the room name is a hash of a secret that bridge holds.

**Push alerts** (`src/index.js`). Turns a request from a bridge into an Apple push notification:

- `POST /v1/notify` takes the kind of event, up to 10 device tokens, the computer's name and, optionally, one line of detail. It signs the request with the app's Apple push key and forwards it to Apple.
- It has no database and does not log request contents.
- `GET /health` answers `{"ok": true}`.

## Test

```bash
node test.mjs
```

## Deploy

```bash
npx wrangler deploy
```

Then set the two secrets (once):

```bash
npx wrangler secret put APNS_KEY_ID
```

```bash
npx wrangler secret put APNS_PRIVATE_KEY
```

`APNS_KEY_ID` is the 10-character ID of an Apple push key. `APNS_PRIVATE_KEY` is the contents of its `.p8` file. Never commit either.
