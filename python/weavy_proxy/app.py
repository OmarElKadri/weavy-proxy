"""FastAPI app exposing the OpenAI-compatible surface."""

from __future__ import annotations

import json
import os
import time

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from pydantic import BaseModel

from . import __version__
from .catalog import fetch_definitions, find_model_def, list_image_models
from .config import load_config
from .runs import upload_asset
from .session import WeavyAuthError, WeavyBlocked, WeavySession


class GenerationRequest(BaseModel):
    model: str | None = None
    prompt: str
    size: str | None = None
    n: int | None = 1


def create_app(config_path=None) -> FastAPI:
    cfg = load_config(config_path)
    session = WeavySession(cfg)
    proxy_key = os.environ.get("WEAVY_PROXY_KEY") or cfg.get("proxy_key")
    app = FastAPI(title="weavy-proxy", version=__version__)

    @app.middleware("http")
    async def _key_gate(request: Request, call_next):
        if proxy_key and request.headers.get("authorization") != f"Bearer {proxy_key}":
            return Response(
                '{"error":{"message":"Invalid proxy key","type":"invalid_request_error"}}',
                status_code=401, media_type="application/json",
            )
        return await call_next(request)

    def _resolve(model_query: str | None) -> dict:
        defs = fetch_definitions(session)
        defn = find_model_def(defs, model_query) if model_query else None
        if not defn:
            names = [d["data"].get("name") for d in list_image_models(defs)[:12]]
            raise _HttpError(400, f'Unknown model "{model_query or ""}". Try one of: {", ".join(n for n in names if n)}')
        return defn

    class _HttpError(Exception):
        def __init__(self, status: int, message: str):
            self.status = status
            self.message = message

    @app.exception_handler(_HttpError)
    async def _http_error_handler(_, exc: _HttpError):
        return Response(
            json.dumps({"error": {"message": exc.message, "type": "api_error"}}),
            status_code=exc.status, media_type="application/json",
        )

    @app.get("/healthz")
    def healthz():
        out: dict = {"status": "ok", "session": "authorized", "credits": None}
        try:
            out["credits"] = session.get_credits()["credits"]
        except Exception as e:
            out["session"] = f"error: {e}"
        return out

    @app.get("/v1/models")
    def models():
        defs = fetch_definitions(session)
        return {"object": "list", "data": list_image_models(defs)}

    @app.get("/v1/credits")
    def credits():
        return {"object": "weavy_credits", **session.get_credits()}

    def _run_generation(body: GenerationRequest):
        defn = _resolve(body.model)
        size = _map_size(body.size, defn)
        from .runs import run_model
        result = run_model(session, defn, prompt=body.prompt, n=body.n or 1, **size)
        return {
            "created": int(time.time()),
            "data": [{"url": u} for u in result["urls"]],
            "weavy_credits_remaining": result["credits"],
        }

    def _map_size(size, defn):
        from .nodes import map_size
        return map_size(size, defn)

    @app.post("/v1/images/generations")
    def generations(body: GenerationRequest):
        if not body.prompt:
            return _json_error(400, "prompt is required")
        return _run_generation(body)

    @app.post("/v1/images/edits")
    async def edits(
        prompt: str = Form(...),
        image: UploadFile = File(...),
        model: str | None = Form(None),
    ):
        defn = _resolve(model or "Nano Banana 2.1")
        data = await image.read()
        url = upload_asset(session, image.filename, data, image.content_type or "image/png")
        from .runs import run_model
        result = run_model(session, defn, prompt=prompt, image_url=url, n=1)
        return {
            "created": int(time.time()),
            "data": [{"url": u} for u in result["urls"]],
            "weavy_credits_remaining": result["credits"],
        }

    def _json_error(status: int, message: str):
        return Response(
            json.dumps({"error": {"message": message, "type": "invalid_request_error"}}),
            status_code=status, media_type="application/json",
        )

    @app.exception_handler(WeavyBlocked)
    async def _blocked_handler(_, exc: WeavyBlocked):
        return Response(
            json.dumps({"error": {"message": str(exc), "type": "blocked"}}),
            status_code=502, media_type="application/json",
        )

    @app.exception_handler(WeavyAuthError)
    async def _auth_handler(_, exc: WeavyAuthError):
        return Response(
            json.dumps({"error": {"message": str(exc), "type": "auth_error"}}),
            status_code=401, media_type="application/json",
        )

    return app
