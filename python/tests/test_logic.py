"""Tests for the pure logic: curl parsing, refresh-token extraction, node building, size mapping."""

import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate_weavy_config import extract_refresh_token, parse_curl  # noqa: E402
from weavy_proxy.nodes import build_kind, build_model_node, map_size  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "banana-def.json").read_text(encoding="utf-8"))

SAMPLE_CURL = (
    "curl 'https://api.weavy.ai/api/v1/models/prices' \\\n"
    "  -H 'accept: application/json, text/plain, */*' \\\n"
    "  -H 'cookie: cf_clearance=ABC123; __cf_bm=DEF456' \\\n"
    "  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/154.0.0.0 Safari/537.36' \\\n"
    "  -H 'x-app-version: 4.1.1678';"
)


def test_parse_curl_extracts_cookie_and_ua():
    parsed = parse_curl(SAMPLE_CURL)
    assert parsed["cookie_header"] == "cf_clearance=ABC123; __cf_bm=DEF456"
    assert "Chrome/154" in parsed["user_agent"]
    assert parsed["url"].startswith("https://api.weavy.ai/")


def test_extract_refresh_token_json_and_bare():
    j = extract_refresh_token('{\n  "email": "a@b.c",\n  "refresh_token": "AMf-vBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n}')
    assert j.startswith("AMf-")
    bare = extract_refresh_token("AMf-vByyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy")
    assert bare.startswith("AMf-")
    assert extract_refresh_token("nothing here") is None


def test_build_model_node_shape():
    node = build_model_node(FIXTURE, prompt="a red panda", aspect="1:1", resolution="1K")
    d = node["data"]
    assert node["type"] == "custommodelV2" and node["isModel"] is True
    assert d["params"]["prompt"] == "a red panda"
    assert d["model"]["name"] == "fal-ai/nano-banana-2/edit"
    kind = d["kind"]
    assert kind["type"] == "wildcard"
    assert kind["model"]["name"] == "fal-ai/nano-banana-2/edit"
    input_ids = [slot[0]["id"] for slot in kind["inputs"]]
    assert "prompt" in input_ids and "image_1" in input_ids
    prompt_slot = next(s for s in kind["inputs"] if s[0]["id"] == "prompt")
    assert prompt_slot[1] is None  # unconnected -> value comes from params
    param_ids = [p[0]["id"] for p in kind["parameters"]]
    assert "resolution" in param_ids and "model" in param_ids
    res_param = next(p for p in kind["parameters"] if p[0]["id"] == "resolution")
    assert res_param[1] == {"type": "value", "data": {"type": "string", "value": "1K"}}
    # seed is a structured param and must be skipped in kind.parameters
    assert "seed" not in param_ids


def test_build_kind_keeps_defaults_for_unspecified():
    node = build_model_node(FIXTURE, prompt="x")
    assert node["data"]["params"]["output_format"] == "png"
    assert node["data"]["params"]["thinking_level"] == "Minimal"


def test_map_size_matches_model_enums():
    m = map_size("1024x1024", FIXTURE)
    assert m["aspect"] == "1:1"
    assert m["resolution"] == "1K"
    m2 = map_size("1792x1024", FIXTURE)
    assert m2["aspect"] == "16:9"
    assert map_size("bogus", FIXTURE) == {}
