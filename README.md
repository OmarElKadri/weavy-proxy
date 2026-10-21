# weavy-proxy

An **OpenAI-compatible image generation API** that runs on top of your logged-in
[Weavy](https://app.weavy.ai) (Figma Weave) session. Point any OpenAI SDK, script, or tool at
`http://127.0.0.1:8787/v1` and generate/edit images with Weavy's 80+ models
(Nano Banana 2.1, Flux 3, Seedream, Ideogram, GPT Image 2.5, ...) using your existing credits.

No Weavy API key or reverse-engineered auth token is stored anywhere. All Weavy calls execute
**inside your real browser** via the Chrome DevTools Protocol, so Cloudflare sees a genuine browser.

```
your script ──HTTP──> weavy-proxy (127.0.0.1:8787) ──CDP──> Brave (signed into app.weavy.ai) ──HTTPS──> api.weavy.ai
```

## Requirements

- Windows + Node.js 18+ (tested with Node 26)
- Brave (or Chrome) installed at the default location
- A Weavy account with credits

## First-time setup

1. Start the dedicated browser profile (opens Brave with remote debugging on port 9222):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start-browser.ps1
   ```

2. **Sign in** at app.weavy.ai in the window that opened (Google sign-in). You only do this once;
   the session persists in the `.browser-profile\` directory next to this folder.

## Running

```powershell
node .\weavy-proxy.mjs
```

Leave the browser window open while the proxy runs. Verify with:

```powershell
curl http://127.0.0.1:8787/healthz
# {"status":"ok","session":"authorized","credits":129,...}
```

## API

All endpoints accept/return JSON. If the `WEAVY_PROXY_KEY` environment variable is set, every
request must send `Authorization: Bearer <key>`.

### `POST /v1/images/generations`

Text-to-image. OpenAI-compatible request/response.

| field  | type   | notes                                                        |
|--------|--------|--------------------------------------------------------------|
| model  | string | model display name, e.g. `"Nano Banana 2.1"` (see `/v1/models`) |
| prompt | string | required                                                     |
| size   | string | optional `"WIDTHxHEIGHT"`, mapped to the model's nearest aspect ratio / resolution tier |
| n      | number | optional, 1-4 images                                         |

```powershell
curl -X POST http://127.0.0.1:8787/v1/images/generations `
  -H "content-type: application/json" `
  --data-binary '{"model":"Nano Banana 2.1","prompt":"a red panda on a skateboard","size":"1024x1024"}'
```

Response:

```json
{
  "created": 1792540375,
  "data": [{ "url": "https://media.weavy.ai/image/upload/.../xxxx.png" }],
  "weavy_credits_remaining": 129
}
```

`weavy_credits_remaining` is a non-OpenAI extra field: your balance right after the run.

### `POST /v1/images/edits`

Image editing (multipart, like OpenAI's edits endpoint).

| field  | type   | notes                          |
|--------|--------|--------------------------------|
| image  | file   | required, the image to edit    |
| prompt | string | required, the edit instruction |
| model  | string | optional, defaults to `"Nano Banana 2.1"` |

```powershell
curl -X POST http://127.0.0.1:8787/v1/images/edits `
  -F "model=Nano Banana 2.1" `
  -F "prompt=Make it bright daytime, keep everything else" `
  -F "image=@C:\path\to\image.png"
```

Response: same shape as `/v1/images/generations`.

### `GET /v1/models`

Lists every image-capable model in your Weavy catalog.

```json
{ "object": "list", "data": [ { "id": "Nano Banana 2.1", "object": "model", "owned_by": "fal_imported:fal-ai/nano-banana-2/edit" }, ... ] }
```

Use `data[].id` verbatim as the `model` field. Partial/fuzzy names usually resolve too
(`"nano banana 2.1"`, `"gpt image"`, ...). Note: Weavy plan-gates certain models regardless of
credits — on a Free account **all OpenAI models** (`GPT Image 2.5`, `ChatGPT Images 2.0`,
`ChatGPT Images 2.0 Edit`) return
`"This model is not available on the Free plan. Upgrade your plan to run it."`
(verified 2026-10-21), while fal.ai models like Nano Banana 2.1 and Flux work on Free.

### `GET /v1/credits`

Your current Weavy balance (read from `GET /api/v1/users` inside the browser session):

```json
{
  "object": "weavy_credits",
  "email": "you@gmail.com",
  "name": "Your Name",
  "plan": "Free",
  "credits": 129,
  "allocated": 150,
  "usage": 21,
  "renews_at": "2026-11-21T23:25:58.050Z",
  "workspace": "Your Workspace"
}
```

The same number is also attached to every generation response (`weavy_credits_remaining`) and
to `/healthz` (`credits`).

### `GET /healthz`

Liveness + session check. `session` is `authorized` when the browser's Weavy login is usable,
`unauthorized` if you need to sign in again. `credits` is your current balance (null while unauthorized).

## How it works (per generation)

1. Resolves the model name against Weavy's public node definitions
   (`/api/v1/node-definitions/public`, cached 24 h in `model-definitions.json`).
2. Ensures a **proxy-owned flow** exists (`POST /api/v1/recipes/create` — created once, reused,
   id persisted in `proxy-flows.json`). Your own flows are never touched.
3. Builds a `custommodelV2` node from the model's definition (handles, params, schema and the
   editor's `kind` wildcard descriptor) and saves it into the flow (`POST /recipes/:id/save`).
4. Executes it (`POST /api/v1/batches/recipes/:id/execute`, `numberOfRuns: n`), then polls
   `/batches/:id/status` until `COMPLETED` and extracts `nodeRuns[].result[].url`.
5. All HTTP calls run via `page.evaluate(fetch(...))` inside the real browser tab and carry the
   Firebase ID token read from IndexedDB (auto-refreshed on 401/403).

## Configuration

`config.json`:

```json
{ "port": 8787, "cdpUrl": "http://127.0.0.1:9222" }
```

Environment variables:

| var               | meaning                                              |
|-------------------|------------------------------------------------------|
| `WEAVY_PROXY_KEY` | if set, required as `Authorization: Bearer <value>`  |

## Files

| file                     | purpose                                          |
|--------------------------|--------------------------------------------------|
| `weavy-proxy.mjs`        | the proxy server                                 |
| `start-browser.ps1`      | launches the dedicated Brave profile with CDP    |
| `config.json`            | port / CDP url                                   |
| `model-definitions.json` | cached model catalog (auto-refreshed daily)      |
| `proxy-flows.json`       | id of the proxy-owned Weavy flow                 |
| `.browser-profile\`      | Brave profile holding your Weavy login           |

## Troubleshooting

| symptom                                                        | fix                                                                     |
|----------------------------------------------------------------|-------------------------------------------------------------------------|
| `/healthz` says `unauthorized`                                 | sign in again at app.weavy.ai in the proxy browser window               |
| `Cannot reach browser CDP at ...`                              | run `start-browser.ps1` first                                           |
| `This model is not available on the Free plan`                 | that model needs a paid Weavy plan; pick another model                  |
| `Unable to run this workflow`                                  | should not happen any more (save-before-execute is built in); report it |
| wrong/failed runs                                              | check the server console log; status polls are logged with progress     |

## Caveats

- Every generation **spends your Weavy credits** and is attributable to your account.
- Automated use may violate Weavy's Terms of Service; this is a reverse-engineering
  demonstration, use at your own risk.
- The browser profile contains a live login session — do not share the folder.
