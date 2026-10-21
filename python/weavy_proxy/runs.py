"""Run pipeline: create proxy-owned flow -> save node -> execute batch -> poll -> extract URLs."""

from __future__ import annotations

import json
import re
import time
import uuid

POLL_SECONDS = 2.0
RUN_TIMEOUT = 6 * 60

MEDIA_RE = re.compile(r"https://(?:media\.weavy\.ai|res\.cloudinary\.com|d[0-9a-z]+\.cloudfront\.net)/[^\"\\\s]+")
ANY_URL_RE = re.compile(r"https://[^\"\\\s]+")


class RunError(RuntimeError):
    pass


def ensure_flow(session, cache_path="weavy-flow-cache.json") -> str:
    try:
        flow_id = json.loads(open(cache_path, encoding="utf-8").read())["flowId"]
        return flow_id
    except Exception:
        pass
    created = session.post_json("/api/v1/recipes/create", {})
    flow_id = created.get("id") or created.get("recipeId") or (created.get("recipe") or {}).get("id")
    if not flow_id:
        raise RunError(f"recipes/create returned no id: {json.dumps(created)[:200]}")
    open(cache_path, "w", encoding="utf-8").write(json.dumps({"flowId": flow_id}))
    return flow_id


def extract_results(node_runs: list[dict], limit: int, exclude: list[str]) -> list[str]:
    def excluded(u: str) -> bool:
        return any(u == x or u.split("?")[0] == x.split("?")[0] for x in exclude)

    out: list[str] = []
    for nr in node_runs:
        res = nr.get("result")
        arr = res if isinstance(res, list) else ([res] if res else [])
        for r in arr:
            u = r if isinstance(r, str) else (r or {}).get("url") or (r or {}).get("originalUrl") or (r or {}).get("viewUrl")
            if isinstance(u, str) and u.startswith("https://") and not excluded(u):
                out.append(u)
    if not out:
        blob = json.dumps([nr.get("result") for nr in node_runs])
        out = [u for u in MEDIA_RE.findall(blob) if not excluded(u)]
        if not out:
            out = [u for u in ANY_URL_RE.findall(blob) if not excluded(u) and re.search(r"image|media|\.png|\.jpe?g|\.webp", u, re.I)]
    seen: set[str] = set()
    unique = [u for u in out if not (u in seen or seen.add(u))]
    return unique[:limit]


def run_model(session, defn: dict, *, prompt, image_url=None, aspect=None, resolution=None, n=1,
              flow_cache="weavy-flow-cache.json"):
    from .nodes import build_model_node

    flow_id = ensure_flow(session, flow_cache)
    node = build_model_node(defn, prompt=prompt, image_url=image_url, aspect=aspect, resolution=resolution)

    # the server rejects execute for nodes that were never persisted in the recipe
    try:
        session.request("POST", f"/api/v1/recipes/{flow_id}/save",
                        json_body={"name": "proxy", "nodes": [node], "edges": []}, recipe_id=flow_id)
    except RuntimeError as e:
        print(f"[weavy] save warning: {e}")

    body = {"numberOfRuns": max(1, min(int(n or 1), 4)), "nodes": [node], "edges": []}
    started = session.post_json(f"/api/v1/batches/recipes/{flow_id}/execute", body, recipe_id=flow_id)
    batch_id = started.get("batchId")
    if not batch_id:
        raise RunError(f"No batchId in execute response: {json.dumps(started)[:300]}")
    print(f"[weavy] batch started: {batch_id} runs: {len(started.get('recipeRuns') or [])}")

    deadline = time.time() + RUN_TIMEOUT
    last_progress = ""
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            st = session.get_json(f"/api/v1/batches/recipes/{flow_id}/batches/{batch_id}/status", recipe_id=flow_id)
        except RuntimeError as e:
            print(f"[weavy] status poll error (will retry): {e}")
            continue
        runs = st.get("recipeRuns") or []
        node_runs = [nr for r in runs for nr in (r.get("nodeRuns") or [])]
        states = [(nr.get("status") or "").upper() for nr in node_runs]
        progress = ", ".join(
            f"{nr.get('status')}{(' ' + str(nr['progress']) + '%') if nr.get('progress') is not None else ''}"
            for nr in node_runs
        )
        if progress and progress != last_progress:
            last_progress = progress
            print(f"[weavy] progress: {progress}")

        failed = next((r for r in runs if r.get("error")), None)
        if failed or any("FAIL" in s or "ERROR" in s or "CANCEL" in s for s in states):
            err_text = failed.get("error") if failed else json.dumps(
                [nr for nr in node_runs if any(t in (nr.get("status") or "").upper() for t in ("FAIL", "ERROR", "CANCEL"))]
            )[:400]
            raise RunError(f"Weavy run failed: {err_text}")

        if states and all(("COMPLETED" in s or "SUCCEED" in s) for s in states):
            urls = extract_results(node_runs, body["numberOfRuns"], [image_url] if image_url else [])
            print(f"[weavy] run completed; urls: {len(urls)}")
            if not urls:
                raise RunError("Run completed but no media URL found: " + json.dumps(node_runs)[:400])
            return {"urls": urls, "credits": st.get("remainingCredits", st.get("userRemainingCredits"))}
    raise RunError("Timed out waiting for Weavy run")


def upload_asset(session, filename: str, data: bytes, content_type: str = "image/png") -> str:
    r = session.request(
        "POST",
        "/api/v1/assets/upload-asset?runType=workflowExecution",
        files={"file": (filename or "image.png", data, content_type)},
        timeout=300,
    )
    j = r.json()
    asset = j.get("asset") or j
    url = (asset or {}).get("url") or (asset or {}).get("secure_url")
    if not url:
        blob = json.dumps(j)
        m = MEDIA_RE.search(blob)
        url = m.group(0) if m else None
    if not url:
        raise RunError(f"upload-asset returned no URL: {blob[:300]}")
    return url
