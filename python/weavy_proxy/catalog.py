"""Model catalog: fetch Weavy's public node definitions and resolve model names."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

CACHE_FILE = Path("weavy-definition-cache.json")
CACHE_TTL = 24 * 3600

FAL_INFERENCE_ENDPOINTS = ("",)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _squash(s: str) -> str:
    return _norm(s).replace(" ", "")


def fetch_definitions(session) -> dict:
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if cache.get("fetchedAt") and time.time() - cache["fetchedAt"] < CACHE_TTL:
                return cache
        except Exception:
            pass
    menu = session.get_json("/api/v1/node-definitions/menu?includeMigrated=true")
    pub = session.get_json("/api/v1/node-definitions/public")
    defs = {"menu": menu, "public": pub, "fetchedAt": time.time()}
    CACHE_FILE.write_text(json.dumps(defs), encoding="utf-8")
    return defs


def collect_model_defs(definitions: dict) -> list[dict]:
    """Full model definitions look like:
    {id, type:'custommodelV2', isModel:true, data:{name, model:{name,service,version}, handles, params, schema}}
    """
    out: list[dict] = []

    def visit(v):
        if isinstance(v, list):
            for item in v:
                visit(item)
        elif isinstance(v, dict):
            d = v.get("data")
            if (
                v.get("type") == "custommodelV2"
                and isinstance(d, dict)
                and isinstance(d.get("model"), dict)
                and d["model"].get("name")
            ):
                out.append(v)
            for k in v:
                visit(v[k])

    visit(definitions)
    return out


def find_model_def(definitions: dict, query: str) -> dict | None:
    q = _norm(query)
    qs = _squash(query)
    best, best_score = None, 0
    for d in collect_model_defs(definitions):
        dN = d["data"]
        names = [dN.get("name"), (dN.get("menu") or {}).get("displayName"), dN["model"].get("name")]
        hay = _norm(" ".join(str(n) for n in names if n))
        hay_s = _squash(" ".join(str(n) for n in names if n))
        score = 0
        if dN.get("name") and _norm(dN["name"]) == q:
            score += 100
        if _squash(dN.get("name") or "") == qs:
            score += 100
        if _squash(dN["model"].get("name") or "") == qs:
            score += 90
        if qs and len(qs) >= 4 and qs in hay_s:
            score += 40
        for tok in q.split():
            if tok and tok in hay:
                score += len(tok)
        if score > best_score:
            best, best_score = d, score
    return best


def list_image_models(definitions: dict) -> list[dict]:
    models = []
    for d in collect_model_defs(definitions):
        meta = d.get("metadata") or {}
        if isinstance(meta.get("outputTypes"), list) and "image" in meta["outputTypes"]:
            dN = d["data"]
            models.append({
                "id": dN.get("name"),
                "object": "model",
                "owned_by": f"{dN['model'].get('service')}:{dN['model'].get('name')}",
            })
    return models
