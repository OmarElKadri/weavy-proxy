"""WeavySession — authenticated HTTP against api.weavy.ai without a live browser.

How it stays alive:
- curl_cffi impersonates Chrome's TLS fingerprint, which (together with the captured
  cf_clearance/__cf_bm cookies and a matching User-Agent) passes Weavy's Cloudflare edge.
- The session cookie jar keeps `__cf_bm` fresh automatically (Cloudflare rotates it on
  every response; we just keep reusing the jar).
- The Firebase ID token (Bearer) expires hourly; we mint a new one from the long-lived
  refresh token via securetoken.googleapis.com (Google-hosted, no Cloudflare involved).
  Google rotates the refresh token on each use, and we persist the latest one.

If Cloudflare ever hard-blocks us (expired cf_clearance bound to IP/fingerprint), requests
fail with WeavyBlocked and the fix is re-running generate_weavy_config.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

from curl_cffi.requests import Session

API_BASE = "https://api.weavy.ai"
APP_BASE = "https://app.weavy.ai"
SECURETOKEN = "https://securetoken.googleapis.com/v1/token"
UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# matches the public web key inside the app bundle (same regex used at generation time)
_KEY_RE = re.compile(r'VITE_FIREBASE_API_KEY:"([^"]+)"')


class WeavyBlocked(RuntimeError):
    """Cloudflare rejected the request — the captured session material is stale."""


class WeavyAuthError(RuntimeError):
    """Weavy rejected the credentials (e.g. signed out, refresh token revoked)."""


def _parse_cookie_header(header: str) -> list[tuple[str, str]]:
    pairs = []
    for pair in header.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        pairs.append((name.strip(), value.strip()))
    return pairs


class WeavySession:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.user_agent = cfg.get("user_agent") or UA_FALLBACK
        self.refresh_token = cfg["refresh_token"]
        self.firebase_api_key = cfg.get("firebase_api_key")
        self.id_token: str | None = None
        self.token_expires_at = 0.0
        self._lock = threading.Lock()
        self.cdp_url = cfg.get("cdpUrl") or os.environ.get("WEAVY_CDP_URL") or "http://127.0.0.1:9222"

        self.http = Session(impersonate="chrome")
        self._seed_cookies(cfg["cookie_header"])
        # refresh immediately so we do not rely on the (stale) captured ID token
        self.refresh_id_token()

    # ---------- cookies ----------
    def _seed_cookies(self, cookie_header: str) -> None:
        for name, value in _parse_cookie_header(cookie_header):
            self.http.cookies.set(name, value, domain=".weavy.ai", path="/")

    def refresh_cookies_via_cdp(self) -> bool:
        """Pull fresh cookies from a real browser over CDP (browser target, no playwright).

        Cloudflare rotates clearance for api.weavy.ai and binds it to the live browser, so a
        static captured cookie only rides a grace window. When a Chrome/Brave with
        --remote-debugging-port is reachable (any window; it does not carry the traffic), we
        re-import its cookies and keep going. Returns True on success.
        """
        try:
            import urllib.request

            import websocket  # websocket-client

            with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=5) as resp:
                ws_url = json.load(resp)["webSocketDebuggerUrl"]
            # Chrome rejects WebSocket handshakes carrying an Origin header unless
            # --remote-allow-origins is set; suppress it (same as playwright does).
            ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
            try:
                ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == 1:
                        break
            finally:
                ws.close()
            cookies = (msg.get("result") or {}).get("cookies") or []
            weavy = [c for c in cookies if c.get("domain", "").endswith("weavy.ai")]
            if not weavy:
                return False
            header = "; ".join(f"{c['name']}={c['value']}" for c in weavy)
            # set() overwrites same-name entries on the same domain/path; no need to clear
            self._seed_cookies(header)
            self.cfg["cookie_header"] = header
            print(f"[weavy] refreshed {len(weavy)} cookies via CDP ({self.cdp_url})")
            return True
        except Exception as e:
            print(f"[weavy] CDP cookie refresh unavailable ({e})", file=sys.stderr)
            return False

    # ---------- auth ----------
    def _extract_api_key(self) -> str:
        if self.firebase_api_key:
            return self.firebase_api_key
        # fallback: pull the public web key out of the app bundle (exactly what the browser loads)
        html = self.http.get(f"{APP_BASE}/flows", headers={"user-agent": self.user_agent}, timeout=30).text
        for m in re.finditer(r'(?:src|href)="(/assets/[A-Za-z0-9_.-]+\.js)"', html):
            url = f"{APP_BASE}{m.group(1)}"
            try:
                body = self.http.get(url, headers={"user-agent": self.user_agent}, timeout=60).text
            except Exception:
                continue
            hit = _KEY_RE.search(body)
            if hit:
                self.firebase_api_key = hit.group(1)
                return self.firebase_api_key
        raise WeavyAuthError(
            "Could not determine the Firebase web key. Set \"firebase_api_key\" in weavy_config.json "
            "(it is public; see the README for where to find it)."
        )

    def refresh_id_token(self) -> None:
        """Exchange the refresh token for a fresh ID token (Google endpoint, no Cloudflare)."""
        with self._lock:
            key = self._extract_api_key()
            r = self.http.post(
                SECURETOKEN,
                params={"key": key},
                json={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                headers={"user-agent": self.user_agent},
                timeout=30,
            )
            if r.status_code != 200:
                raise WeavyAuthError(
                    f"Firebase token refresh failed ({r.status_code}). Your refresh token was likely "
                    "revoked (signed out elsewhere). Re-run generate_weavy_config.py."
                )
            j = r.json()
            self.id_token = j["id_token"]
            self.refresh_token = j.get("refresh_token", self.refresh_token)  # Google rotates it
            try:
                ttl = int(j.get("expires_in", 3600))
            except (TypeError, ValueError):
                ttl = 3600
            self.token_expires_at = time.time() + ttl - 300  # refresh 5 min early

    def _bearer(self) -> str:
        if not self.id_token or time.time() >= self.token_expires_at:
            self.refresh_id_token()
        return f"Bearer {self.id_token}"

    # ---------- requests ----------
    def _headers(self, extra: dict | None = None, recipe_id: str | None = None) -> dict:
        m = re.search(r"Chrome/(\d+)", self.user_agent)
        chrome_v = m.group(1) if m else "124"
        h = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://app.weavy.ai",
            "referer": "https://app.weavy.ai/",
            "user-agent": self.user_agent,
            "sec-ch-ua": f'"Chromium";v="{chrome_v}", "Google Chrome";v="{chrome_v}", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "x-app-version": "4.1.1678",
            "x-weavy-auth-provider": "firebase",
            "x-wv-tier-cookie": "1",
            "authorization": self._bearer(),
        }
        if recipe_id:
            h["x-app-recipeid"] = recipe_id
        if extra:
            h.update(extra)
        return h

    def _rescue_via_cdp(self) -> bool:
        """Throttled CDP cookie rescue (at most once per minute)."""
        if time.time() - getattr(self, "_last_cdp_rescue", 0) < 60:
            return False
        self._last_cdp_rescue = time.time()
        if not self.refresh_cookies_via_cdp():
            return False
        return True

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body=None,
        files=None,
        headers_extra: dict | None = None,
        recipe_id: str | None = None,
        timeout: int = 120,
        _auth_retried: bool = False,
    ):
        url = path if path.startswith("http") else API_BASE + path
        headers = self._headers(headers_extra, recipe_id)
        if json_body is not None:
            headers["content-type"] = "application/json"
        r = self.http.request(
            method, url, headers=headers,
            json=json_body if json_body is not None else None,
            files=files, timeout=timeout,
        )
        if r.status_code in (401, 403) and not _auth_retried:
            body = (r.text or "")[:200]
            if re.search(r"^\s*<(!doctype|html)", body, re.I) or "cf-mitigated" in r.headers or "challenge" in body.lower():
                # Cloudflare rotated clearance. A real browser re-earns it silently; import its
                # cookies if one is reachable on the CDP port, then retry once.
                if self._rescue_via_cdp():
                    return self.request(
                        method, path, json_body=json_body, files=files, headers_extra=headers_extra,
                        recipe_id=recipe_id, timeout=timeout, _auth_retried=True,
                    )
                raise WeavyBlocked(
                    "Cloudflare rejected the request and no browser is reachable on "
                    f"{self.cdp_url} to refresh cookies. Open app.weavy.ai in the debug-enabled "
                    "browser (or re-run generate_weavy_config.py)."
                )
            # stale/absent Bearer -> refresh once and retry
            self.refresh_id_token()
            return self.request(
                method, path, json_body=json_body, files=files, headers_extra=headers_extra,
                recipe_id=recipe_id, timeout=timeout, _auth_retried=True,
            )
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = j.get("message") or j.get("error") or json.dumps(j)[:300]
            except Exception:
                msg = (r.text or "")[:200].replace("\n", " ")
            err = RuntimeError(f"Weavy API {r.status_code} for {method} {path}: {msg}")
            err.status = r.status_code
            err.body = r.text
            raise err
        return r

    def get_json(self, path: str, **kw):
        return self.request("GET", path, **kw).json()

    def post_json(self, path: str, body, **kw):
        return self.request("POST", path, json_body=body, **kw).json()

    # ---------- account ----------
    def get_credits(self) -> dict:
        u = self.get_json("/api/v1/users")
        ws = u.get("activeWorkspace") or {}
        sub = ws.get("subscription") or {}
        slots = sub.get("creditsList") or [{}]
        slot = slots[0] or {}
        return {
            "email": u.get("email"),
            "name": u.get("name"),
            "plan": sub.get("name") or sub.get("type") or "unknown",
            "credits": sub.get("credits", ws.get("credits")),
            "allocated": slot.get("allocatedAmount", sub.get("monthlyCredits")),
            "usage": ws.get("userWorkspaceCreditsUsage"),
            "renews_at": slot.get("expiresAt"),
            "workspace": ws.get("workspaceName"),
        }
