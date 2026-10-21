"""Generate weavy_config.json from a captured "Copy as cURL" request + a console-snippet paste.

Usage:
  1. On app.weavy.ai (signed in), DevTools -> Network -> click any request to api.weavy.ai
     -> right-click -> Copy -> Copy as cURL (bash). Save it to input_curl.txt.
  2. In the DevTools Console on app.weavy.ai, run the snippet from console-snippet.js
     and save its JSON output to refresh_token.txt.
  3. python generate_weavy_config.py input_curl.txt --refresh-file refresh_token.txt

The generator extracts: the Cookie header (cf_clearance/__cf_bm/...), the User-Agent of the
browser that issued those cookies, your Firebase refresh token, and (best effort) the public
Firebase web key from the app bundle.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

KEY_RE = re.compile(r'VITE_FIREBASE_API_KEY:"([^"]+)"')
REFRESH_RE = re.compile(r'"refresh_token"\s*:\s*"([^"]+)"')
UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)


def tokenize_curl(text: str) -> list[str]:
    """Join bash line continuations and tokenize a copied curl command."""
    text = text.replace("\\\n", " ").replace("\\\r\n", " ")
    text = text.strip()
    if text.startswith("curl "):
        text = text[5:]
    # "Copy as cURL (bash)" uses single quotes; shlex handles them.
    return shlex.split(text)


def parse_curl(text: str) -> dict:
    tokens = tokenize_curl(text)
    out = {"url": None, "headers": {}, "cookie_header": None, "user_agent": None}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header") and i + 1 < len(tokens):
            header = tokens[i + 1]
            name, _, value = header.partition(":")
            out["headers"][name.strip().lower()] = value.strip()
            i += 2
            continue
        if t in ("--data", "--data-raw", "--data-binary", "-d", "-d/") and i + 1 < len(tokens):
            out["body"] = tokens[i + 1]
            i += 2
            continue
        if not t.startswith("-") and out["url"] is None and t.startswith("http"):
            out["url"] = t
        i += 1
    out["cookie_header"] = out["headers"].get("cookie")
    out["user_agent"] = out["headers"].get("user-agent")
    return out


def extract_refresh_token(text: str) -> str | None:
    m = REFRESH_RE.search(text)
    if m:
        return m.group(1)
    # accept a bare token paste too (Firebase refresh tokens start with "AMf-")
    bare = re.search(r"\b(AMf-[A-Za-z0-9_-]{40,})\b", text)
    return bare.group(1) if bare else None


def scrape_firebase_api_key(cookie_header: str, user_agent: str) -> str | None:
    try:
        from curl_cffi.requests import Session

        s = Session(impersonate="chrome")
        headers = {"user-agent": user_agent, "cookie": cookie_header} if cookie_header else {"user-agent": user_agent}
        html = s.get("https://app.weavy.ai/flows", headers=headers, timeout=30).text
        for m in re.finditer(r'(?:src|href)="(/assets/[A-Za-z0-9_.-]+\.js)"', html):
            try:
                body = s.get(f"https://app.weavy.ai{m.group(1)}", headers=headers, timeout=60).text
            except Exception:
                continue
            hit = KEY_RE.search(body)
            if hit:
                return hit.group(1)
    except Exception as e:
        print(f"[warn] could not auto-extract firebase key: {e}", file=sys.stderr)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate weavy_config.json for the browserless proxy")
    ap.add_argument("curl_file", help="input_curl.txt — a 'Copy as cURL (bash)' of any api.weavy.ai request")
    ap.add_argument("--refresh-file", help="file containing the console-snippet output (refresh token)")
    ap.add_argument("--refresh-paste", help="paste the refresh token directly instead of --refresh-file")
    ap.add_argument("-o", "--output", default="weavy_config.json")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    curl_text = Path(args.curl_file).read_text(encoding="utf-8")
    parsed = parse_curl(curl_text)
    if not parsed["cookie_header"]:
        raise SystemExit("No Cookie header found in the curl. Copy a request to api.weavy.ai (not the page itself).")

    refresh_text = ""
    if args.refresh_file:
        refresh_text = Path(args.refresh_file).read_text(encoding="utf-8")
    elif args.refresh_paste:
        refresh_text = args.refresh_paste
    else:
        print("Paste the output of console-snippet.js (or a bare refresh token), then press Enter:", file=sys.stderr)
        refresh_text = input()

    refresh_token = extract_refresh_token(refresh_text)
    if not refresh_token:
        raise SystemExit(
            'No refresh token found. Run console-snippet.js in the DevTools console on app.weavy.ai '
            "and paste its output."
        )

    user_agent = parsed["user_agent"] or UA_FALLBACK
    print("Fetching the public Firebase web key from the app bundle (one-time)...", file=sys.stderr)
    api_key = scrape_firebase_api_key(parsed["cookie_header"], user_agent or "")
    if not api_key:
        print(
            "[warn] firebase_api_key not auto-extracted; the proxy will re-try at runtime, or set it "
            'manually in weavy_config.json ("firebase_api_key").',
            file=sys.stderr,
        )

    cfg = {
        "cookie_header": parsed["cookie_header"],
        "user_agent": user_agent,
        "refresh_token": refresh_token,
        "firebase_api_key": api_key,
        "proxy_key": None,
        "port": args.port,
    }
    Path(args.output).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}. Start the proxy with:  python -m weavy_proxy --port {args.port}")
    print("Treat that file like a password — it contains live session material.", file=sys.stderr)


if __name__ == "__main__":
    main()
