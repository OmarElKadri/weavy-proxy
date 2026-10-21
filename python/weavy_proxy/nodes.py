"""Node building: construct the exact custommodelV2 payload Weavy's execute API accepts.

This is a faithful port of the shapes proven against the live API:
- data comes verbatim from the model's public definition (handles/params/schema/model/menu),
- params get the runtime prompt/image/size values,
- data.kind is the editor's "wildcard" descriptor (model + inputs + parameters slots).
The server requires the node to be SAVED into the recipe before execute; runs.py does that.
"""

from __future__ import annotations

import copy
import re
import uuid


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _js_type(v) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def _fit_enum(schema_entry: dict | None, want: str | None):
    """Return the model's own casing for an enum value, or None to keep the default."""
    if not want or not isinstance(schema_entry, dict):
        return None
    if schema_entry.get("type") != "enum" or not isinstance(schema_entry.get("options"), list):
        return None
    for opt in schema_entry["options"]:
        if _norm(opt) == _norm(want):
            return opt
    return None


def build_kind(def_data: dict, params: dict) -> dict:
    inputs = []
    handles_in = (def_data.get("handles") or {}).get("input") or {}
    for _, h in sorted(handles_in.items(), key=lambda kv: kv[1].get("order") or 0):
        inputs.append([
            {
                # the app's captured bodies use the handle label here (e.g. "prompt"), not the uuid
                "id": h.get("label") or h.get("id"),
                "title": h.get("label"),
                "description": h.get("description") or "",
                "validTypes": [h.get("type")],
                "required": bool(h.get("required")),
            },
            None,  # unconnected: values come from params
        ])

    parameters = []
    for key, sch in (def_data.get("schema") or {}).items():
        pv = params.get(key)
        if pv is None or isinstance(pv, dict):
            continue  # skip structured params like seed:{seed,isRandom}
        if sch.get("type") == "enum":
            constraint = {"type": "enum", "options": sch.get("options")}
        else:
            constraint = {"type": sch.get("type")}
        entry = [{"id": key, "title": sch.get("title") or key, "description": sch.get("description") or "", "constraint": constraint}]
        if sch.get("default") is not None:
            entry[0]["defaultValue"] = {"type": _js_type(sch["default"]), "value": sch["default"]}
        entry.append({"type": "value", "data": {"type": _js_type(pv), "value": pv}})
        parameters.append(entry)

    model = def_data["model"]
    return {
        "type": "wildcard",
        "model": {
            "type": "user_defined",
            "name": model.get("name"),
            "version": model.get("version") or model.get("name"),
            "service": model.get("service"),
            "description": def_data.get("description") or "Weavy model",
        },
        "inputs": inputs,
        "parameters": parameters,
    }


def build_model_node(defn: dict, *, prompt=None, image_url=None, aspect=None, resolution=None) -> dict:
    data = copy.deepcopy(defn["data"])
    schema = data.get("schema") or {}
    handles_in = (data.get("handles") or {}).get("input") or {}
    params = dict(data.get("params") or {})

    if prompt is not None and "prompt" in handles_in:
        params["prompt"] = str(prompt)
    if image_url:
        if "image_1" in handles_in:
            params["image_1"] = image_url
        elif "image_urls" in params:
            params["image_urls"] = image_url
    fitted = _fit_enum(schema.get("aspect_ratio"), aspect)
    if fitted:
        params["aspect_ratio"] = fitted
    fitted = _fit_enum(schema.get("resolution"), resolution)
    if fitted:
        params["resolution"] = fitted

    data["params"] = params
    data["kind"] = build_kind(data, params)

    return {
        "id": str(uuid.uuid4()),
        "dragHandle": ".node-header",
        "owner": None,
        "type": defn.get("type") or "custommodelV2",
        "visibility": "private",
        "isModel": True,
        "position": {"x": 170, "y": -440},
        "data": data,
        "generations": [],
        "selectedIndex": 0,
        "cameraLocked": False,
        "renderWidth": 1024,
        "renderHeight": 1024,
        "result": [],
        "output": {},
        "selectedOutput": 0,
        "input": {"prompt": params.get("prompt"), **({"images": [image_url]} if image_url else {})},
        "locked": False,
        "selected": False,
        "width": 460,
        "height": 550,
        "zIndex": 2,
    }


_SIZE_RATIOS = [
    (1.0, "1:1"), (4 / 3, "4:3"), (3 / 2, "3:2"), (16 / 9, "16:9"), (21 / 9, "21:9"),
    (9 / 16, "9:16"), (2 / 3, "2:3"), (3 / 4, "3:4"), (4 / 5, "4:5"), (5 / 4, "5:4"),
]
_RESOLUTIONS = [(600, "512"), (1600, "1K"), (2300, "2K"), (99999, "4K")]


def map_size(size, defn: dict) -> dict:
    """Map an OpenAI-style size ("1024x1024") onto the model's own enum options."""
    if not size or size == "auto":
        return {}
    m = re.fullmatch(r"(\d+)x(\d+)", str(size))
    if not m:
        return {}
    w, h = int(m.group(1)), int(m.group(2))
    ratio = w / h
    schema = defn["data"].get("schema") or {}

    aspect = None
    aspect_opts = [o for o in ((schema.get("aspect_ratio") or {}).get("options") or [])]
    best = None
    for r, name in _SIZE_RATIOS:
        if any(_norm(o) == _norm(name) for o in aspect_opts):
            if best is None or abs(ratio - r) < abs(ratio - best[0]):
                best = (r, name)
    if best:
        aspect = best[1]

    resolution = None
    res_opts = (schema.get("resolution") or {}).get("options") or []
    px = max(w, h)
    for max_px, name in _RESOLUTIONS:
        hit = next((o for o in res_opts if _norm(o) == _norm(name)), None)
        if hit and px <= max_px:
            resolution = hit
            break
        if hit:
            resolution = hit
    return {"aspect": aspect, "resolution": resolution}
