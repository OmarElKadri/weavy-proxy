# weavy-proxy

An **OpenAI-compatible image generation API** that runs on top of your logged-in
[Weavy](https://app.weavy.ai) (Figma Weave) session. Point any OpenAI SDK, script, or tool at
`http://127.0.0.1:8787/v1` and generate/edit images with Weavy's 80+ models
(Nano Banana 2.1, Flux 3, Seedream, Ideogram, GPT Image 2.5, ...) using your existing credits.

Two editions:

| Edition | Path | Needs a browser? | Traffic route |
|---|---|---|---|
| **Node + CDP** (original) | `weavy-proxy.mjs` | Yes, open and signed in | Every API call runs inside the browser page |
| **Python browserless** (`python/`) | `python -m weavy_proxy` | Only for one-time setup; auto-rescues cookies from a CDP browser when Cloudflare rotates | Direct HTTPS with Chrome-impersonating TLS (`curl_cffi`) |

> **Cloudflare reality check** (measured 2026-10-21): api.weavy.ai is protected by rotating,
> browser-bound Cloudflare clearance. A captured cookie + Chrome-impersonating client passes for
> a while, but clearance rotates and then only a real browser (which re-solves challenges
> silently) keeps access. The Python edition therefore ships with a **hybrid** rescue: when a
> request gets challenged, it pulls fresh cookies from any browser exposing CDP on
> `127.0.0.1:9222` (default; configurable). With the debug-enabled browser running, it runs for
> days without touch; without it, re-run the config generator when it blocks.

No Weavy API key or token is stored anywhere by either edition. The Node edition tunnels every
call through the real browser; the Python edition authenticates with a pasted Firebase refresh
token (re-minted hourly against Google) plus Chrome-impersonating TLS.

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

| field  | type      | notes                          |
|--------|-----------|--------------------------------|
| image  | file (1+) | required, the image(s) to edit — repeat the field for multi-image edits (up to 16 on most models); `image[]` accepted as an alias |
| prompt | string    | required, the edit instruction |
| model  | string    | optional, defaults to `"Nano Banana 2.1"` |
| size   | string    | optional, OpenAI-style `"WxH"` (e.g. `1024x1024`, `1536x1152`); mapped onto the model's own aspect-ratio/resolution options; omit for the model default |
| n      | int       | optional, number of images (1–4, default 1) |

```powershell
curl -X POST http://127.0.0.1:8787/v1/images/edits `
  -F "model=Nano Banana 2.1" `
  -F "prompt=Make it bright daytime, keep everything else" `
  -F "image=@C:\path\to\image.png"

# multi-image: pass the image field once per file
curl -X POST http://127.0.0.1:8787/v1/images/edits `
  -F "prompt=Put the subject of the second image into the scene of the first" `
  -F "image=@C:\path\to\scene.png" -F "image=@C:\path\to\subject.png" `
  -F "size=1536x1152"
```

Response: same shape as `/v1/images/generations`.

> Under the hood each upload becomes a Weavy "File" (`import`) node wired into the model node
> with one edge per image and the file object placed in `params.image_1`, `params.image_2`, ...
> — the same graph and execute body the Weavy editor itself produces. A bare URL in `image_1`
> is silently dropped by Weavy's runner (`input.images` stays empty), so this wiring is required.

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

## Browserless edition (`python/`)

Same endpoints, same pipeline (definitions -> proxy-owned flow -> save node -> execute -> poll),
but all traffic goes out as direct HTTPS with a Chrome-impersonating TLS fingerprint — no browser
in the request path.

### One-time setup

1. Create the session material while signed in at app.weavy.ai:
   - DevTools -> Network -> click **any request to `api.weavy.ai`** -> right-click -> Copy ->
     **Copy as cURL (bash)** -> save to `python/input_curl.txt`.
   - In the DevTools **Console**, run `python/console-snippet.js` and save its JSON output to
     `python/refresh_token.txt`.
2. Generate the config and start:

   ```powershell
   cd python
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python generate_weavy_config.py input_curl.txt --refresh-file refresh_token.txt
   python -m weavy_proxy --port 8787
   ```

3. Test:

   ```powershell
   curl http://127.0.0.1:8787/healthz      # {"status":"ok","session":"authorized","credits":129}
   curl http://127.0.0.1:8787/v1/credits
   ```

`weavy_config.json` contains your live session material — it is gitignored and must never be
shared or committed.

### Staying alive

- The Firebase **refresh token** keeps the Bearer token fresh indefinitely (Google endpoint, no
  Cloudflare involved; the token rolls itself on every refresh).
- Cloudflare **clearance cookies** are the fragile part (see the reality check above). The proxy
  auto-rescues them from a browser on `cdpUrl` (default `http://127.0.0.1:9222`) whenever a
  request gets challenged. Launch that browser with `..\start-browser.ps1` and leave it open for
  fully unattended operation — the proxy never sends traffic through it, it only borrows cookies.
- If no browser is reachable, blocked requests fail with a clear message; re-run the generator.

### Tests

```powershell
pip install -r requirements-dev.txt
pytest
```

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
